"""Platform discovery table — breadth-first "what shipped recently" feed.

Populated by the Independent Platform Research worker (via discovery_write), read by
the steward (discovery_search/get), and drained by the research dispatcher — the
deterministic queue drainer in the council-worker (one 'new' row at a time ->
research sweep -> reviewed | failed).

Dedup is by `fingerprint`: re-discovering the same item refreshes its details but
never resets its processing state (so a 'researched' item isn't re-queued).
"""
import re
from uuid import uuid4

from app.database import get_pool

DISCOVERY_CAP = 15                       # max entries a single sweep may write
_VALID_CATEGORY = {"model", "tool", "skill"}
# User/steward-requested topics jump ahead of scout discoveries (default priority 0).
PRIORITY_USER = 100


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "topic"


class DiscoveryError(Exception):
    """Rejected write (cap/enum) — surfaced to the agent so it self-corrects."""


def _serialize(row) -> dict:
    d = dict(row)
    for ts in ("created_at", "updated_at"):
        v = d.get(ts)
        if v is not None and hasattr(v, "isoformat"):
            d[ts] = v.isoformat()
    return d


async def upsert_discoveries(*, entries: list, discovered_by: str | None = None) -> dict:
    """Batch-insert discoveries (≤DISCOVERY_CAP). Dedup on fingerprint: an existing
    row's title/summary/url/released_at/source refresh, but status + finding link are
    preserved. Returns {ok, written:[{fingerprint, action}]}."""
    if not isinstance(entries, list) or not entries:
        raise DiscoveryError("entries must be a non-empty list")
    if len(entries) > DISCOVERY_CAP:
        raise DiscoveryError(
            f"cap exceeded: a sweep may write at most {DISCOVERY_CAP} discoveries; "
            f"got {len(entries)}. Submit only the best few per category.")

    pool = get_pool()
    written: list[dict] = []
    for e in entries:
        category = str(e.get("category") or "").strip()
        title = str(e.get("title") or "").strip()
        fingerprint = str(e.get("fingerprint") or "").strip()
        if category not in _VALID_CATEGORY or not title or not fingerprint:
            raise DiscoveryError(
                f"each entry needs a fingerprint, a title, and category in "
                f"{sorted(_VALID_CATEGORY)} (got category={category!r}, "
                f"title={'set' if title else 'empty'}, fingerprint={fingerprint!r})")
        row = await pool.fetchrow(
            """
            INSERT INTO platform_discoveries
                (id, category, title, summary, url, released_at, source,
                 fingerprint, status, discovered_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'new',$9)
            ON CONFLICT (fingerprint) DO UPDATE SET
                title       = EXCLUDED.title,
                summary     = EXCLUDED.summary,
                url         = EXCLUDED.url,
                released_at = EXCLUDED.released_at,
                source      = EXCLUDED.source,
                updated_at  = now()
            RETURNING (xmax = 0) AS inserted
            """,
            uuid4().hex, category, title,
            str(e.get("summary") or ""), str(e.get("url") or ""),
            (str(e.get("released_at")) if e.get("released_at") else None),
            str(e.get("source") or ""), fingerprint, discovered_by,
        )
        written.append({"fingerprint": fingerprint,
                        "action": "inserted" if row["inserted"] else "refreshed"})
    # Wake the research drainer for every genuinely NEW discovery (a re-discovery that
    # only refreshed details keeps its processing state, so it must NOT re-enqueue).
    if any(w["action"] == "inserted" for w in written):
        try:
            await pool.execute("SELECT pg_notify('discovery_wake', 'insert')")
        except Exception:
            pass  # best-effort; the drainer's periodic poll is the backstop
    return {"ok": True, "written": written}


async def search(*, category: str | None = None, status: str | None = None,
                 limit: int = 50) -> list:
    """List discoveries (lean), newest first. Empty filters = no filter."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT id, category, title, summary, url, released_at, source,
                  status, registry_finding_id, created_at, updated_at
             FROM platform_discoveries
            WHERE ($1::text IS NULL OR category = $1)
              AND ($2::text IS NULL OR status = $2)
            ORDER BY created_at DESC
            LIMIT $3""",
        (category or None), (status or None), max(1, min(int(limit or 50), 200)),
    )
    return [_serialize(r) for r in rows]


async def get(discovery_id: str) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM platform_discoveries WHERE id=$1", discovery_id)
    return _serialize(row) if row else None

async def enqueue_topic(*, kind: str, topic: str, priority: int = PRIORITY_USER) -> dict:
    """User/steward on-demand research request → a HIGH-PRIORITY discovery row the
    drainer picks up next (front of queue; it never preempts an in-flight sweep).

    Idempotent by fingerprint (`<kind>:<slug>`): re-requesting the same topic just
    re-prioritises + re-queues it (so an already reviewed/failed topic can be re-run
    on explicit request), but NEVER yanks a row that's currently 'researching'."""
    kind = (kind or "").strip().lower()
    topic = (topic or "").strip()
    if kind not in _VALID_CATEGORY:
        raise DiscoveryError(f"kind must be one of {sorted(_VALID_CATEGORY)}")
    if not topic:
        raise DiscoveryError("topic is required")
    fingerprint = f"{kind}:{_slug(topic)}"
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO platform_discoveries
            (id, category, title, summary, url, source, fingerprint, status, priority, queued_at)
        VALUES ($1,$2,$3,'','','steward-request',$4,'new',$5, now())
        ON CONFLICT (fingerprint) DO UPDATE SET
            priority   = GREATEST(platform_discoveries.priority, EXCLUDED.priority),
            queued_at  = now(),
            status     = CASE WHEN platform_discoveries.status = 'researching'
                              THEN platform_discoveries.status ELSE 'new' END,
            updated_at = now()
        RETURNING id, status, priority
        """,
        uuid4().hex, kind, topic[:200], fingerprint, int(priority),
    )
    try:
        await pool.execute("SELECT pg_notify('discovery_wake', 'topic')")
    except Exception:
        pass
    return {"ok": True, "discovery_id": row["id"], "status": row["status"],
            "priority": row["priority"], "fingerprint": fingerprint}


# NOTE: claiming a discovery ('new' -> 'researching'), linking its sweep, and marking
# it 'reviewed'/'failed' is owned SOLELY by the research dispatcher (the deterministic
# queue drainer in the council-worker). There is intentionally no claim/mark helper
# here — a second claim path would let something bypass the single-in-flight guarantee.
