"""Platform Research Council v2 — sweep orchestration.

A sweep is one orchestrated unit (the "Run research sweep" button, a scheduled
run, or an on-demand chat request). Flow:

  enqueue_sweep(kinds)        -> create sweep, spawn producers in PARALLEL
  producer registry_write     -> on_producer_submit(): mark producer done; when
                                 ALL producers are in, fan out one INDEPENDENT
                                 reviewer per pending finding (reviewing phase)
  reviewer registry_verdict   -> on_verdict(): when no findings remain pending,
                                 the sweep is done (the steward is woken to
                                 narrate — wired in a later step)

Findings link to a sweep via platform_registry.run_id = sweep id. The post-write
hooks are called from the registry_write / registry_verdict endpoints.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from app.database import get_pool
from app.services.platform_council import worker as _worker
from app.services.platform_council import registry as _registry
from app.services.platform_council import PLATFORM_GOAL_ID, STEWARD_AGENT_ID

log = logging.getLogger(__name__)

# Map a producer's registry source -> its sweep "kind".
_SOURCE_TO_KIND = {"model_research": "model", "tool_research": "tool",
                   "skill_research": "skill"}

# An active sweep older than this with no progress is considered stuck and is
# auto-failed so a new sweep can start (e.g. a producer never called its tool).
_STALE_SECONDS = 30 * 60

ALL_KINDS = ["model", "tool", "skill"]


async def active_sweep() -> dict | None:
    """The latest in-flight sweep, or None. Auto-fails a stale stuck one first."""
    pool = get_pool()
    await pool.execute(
        "UPDATE platform_sweeps SET status='failed', finished_at=NOW() "
        "WHERE status IN ('researching','reviewing') "
        "AND created_at < NOW() - ($1 || ' seconds')::interval",
        str(_STALE_SECONDS),
    )
    row = await pool.fetchrow(
        "SELECT * FROM platform_sweeps WHERE status IN ('researching','reviewing') "
        "ORDER BY created_at DESC LIMIT 1"
    )
    return dict(row) if row else None


async def queue_status() -> dict:
    """Current research-queue status for the steward + UI. busy=True means a sweep
    is in flight (researching or reviewing) and NO new research/review can be
    dispatched until it finishes."""
    sweep = await active_sweep()
    if not sweep:
        return {"busy": False, "status": "idle",
                "message": "No research in progress — new research can be dispatched."}
    phase = sweep["status"]
    kinds = list(sweep.get("kinds") or [])
    done = list(sweep.get("producers_done") or [])
    return {
        "busy": True, "status": phase, "sweep_id": sweep["id"],
        "trigger": sweep["trigger"], "kinds": kinds, "producers_done": done,
        "progress": f"{len(done)}/{len(kinds)}" if kinds else None,
        "message": (f"A research sweep is currently {phase} (trigger={sweep['trigger']}, "
                    f"id {sweep['id'][:8]}). New research/review cannot be dispatched until "
                    f"it finishes."),
    }


async def enqueue_sweep(
    *, kinds: list[str] | None = None, trigger: str = "manual",
    created_by: str | None = None, thread_id: str | None = None, question: str = "",
) -> dict:
    """Start a sweep: spawn the requested producers in parallel. Refuses if one is
    already in flight. kinds defaults to all producers (the full button sweep)."""
    kinds = [k for k in (kinds or ALL_KINDS) if k in _worker.PRODUCER_TEMPLATES]
    if not kinds:
        return {"ok": False, "reason": "no valid producer kinds"}

    existing = await active_sweep()
    if existing:
        return {"ok": False, "reason": "a research sweep is already in progress",
                "sweep_id": existing["id"], "status": existing["status"]}

    pool = get_pool()
    sweep_id = uuid4().hex
    await pool.execute(
        "INSERT INTO platform_sweeps (id, trigger, status, kinds, question, thread_id, created_by) "
        "VALUES ($1, $2, 'researching', $3, $4, $5, $6)",
        sweep_id, trigger, kinds, question or "", thread_id, created_by,
    )

    spawned, failures = [], []
    for kind in kinds:
        try:
            res = await _worker.spawn_producer(kind, run_id=sweep_id, question=question)
        except Exception as exc:  # never leave the sweep stuck on a raise
            res = {"ok": False, "reason": str(exc)}
        (spawned if res.get("ok") else failures).append({"kind": kind, **res})

    if not spawned:  # every producer failed to launch
        await pool.execute(
            "UPDATE platform_sweeps SET status='failed', finished_at=NOW() WHERE id=$1", sweep_id)
        return {"ok": False, "reason": f"all producers failed to start: {failures}",
                "sweep_id": sweep_id}

    log.info("platform: sweep %s started (trigger=%s, kinds=%s, spawned=%d, failed=%d)",
             sweep_id, trigger, kinds, len(spawned), len(failures))
    return {"ok": True, "sweep_id": sweep_id, "kinds": kinds,
            "spawned": [s["kind"] for s in spawned], "failed": [f["kind"] for f in failures]}


async def _run_pending(run_id: str) -> list[dict]:
    """Findings of a sweep that still await an integration verdict."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, source, category, target_role, provider_family, title, summary, detail, evidence "
        "FROM platform_registry WHERE run_id=$1 AND verdict='pending' ORDER BY source, created_at",
        run_id,
    )
    return [dict(r) for r in rows]


async def on_producer_submit(source: str, run_id: str | None) -> None:
    """Called after a producer's registry_write. Marks the producer done; when all
    producers for the sweep are in, transitions to the reviewing phase and fans
    out one independent reviewer per pending finding.

    Resilient to a producer that dropped its run_id: falls back to the active
    sweep that still expects this kind.
    """
    kind = _SOURCE_TO_KIND.get(source)
    if not kind:
        return
    pool = get_pool()

    # Resolve which sweep this submission belongs to (id, or the active sweep
    # still expecting this kind if the producer dropped its run_id).
    sweep_id = None
    if run_id:
        sweep_id = await pool.fetchval(
            "SELECT id FROM platform_sweeps WHERE id=$1 AND status='researching'", run_id)
    if not sweep_id:
        sweep_id = await pool.fetchval(
            "SELECT id FROM platform_sweeps WHERE status='researching' AND $1 = ANY(kinds) "
            "AND NOT ($1 = ANY(producers_done)) ORDER BY created_at DESC LIMIT 1",
            kind,
        )
    if not sweep_id:
        log.info("platform: producer %s submitted with no matching sweep (run=%s) — ad-hoc", source, run_id)
        return

    # Atomically add this kind to producers_done and read back the post-update
    # state — race-safe under the row lock (a concurrent producer's UPDATE
    # serializes and sees this value). DISTINCT keeps a double-submit idempotent.
    row = await pool.fetchrow(
        """
        UPDATE platform_sweeps
           SET producers_done = ARRAY(SELECT DISTINCT unnest(producers_done || $1::text[]))
         WHERE id = $2 AND status = 'researching'
        RETURNING producers_done, kinds
        """,
        [kind], sweep_id,
    )
    if not row:  # status changed under us (already reviewing/failed)
        return
    if set(row["producers_done"]) < set(row["kinds"]):
        log.info("platform: sweep %s — producer %s in (%d/%d)", sweep_id, kind,
                 len(row["producers_done"]), len(row["kinds"]))
        return

    # All producers in. Interactive sweeps hand control back to the steward (it
    # decides to review_spawn, with progress narrated in the goal chat); autonomous
    # sweeps (button/scheduled) auto-fan-out reviewers.
    meta = await pool.fetchrow(
        "SELECT trigger, thread_id, question FROM platform_sweeps WHERE id=$1", sweep_id)
    if meta and meta["trigger"] == "interactive":
        q = meta["question"]
        await _wake_steward(
            meta["thread_id"],
            f"[System] Your on-demand {kind} research"
            + (f" on '{q}'" if q else "")
            + f" just finished and wrote its findings to the registry. Call "
            f"review_spawn(sweep_id='{sweep_id}') to get adoptability verdicts on the new "
            f"findings, then summarize them for the admins and offer to create stories for "
            f"anything adoptable.")
        return
    await begin_review_phase(sweep_id)


async def begin_review_phase(run_id: str) -> None:
    """Fan out one independent reviewer per pending finding. If nothing is pending
    (e.g. all re-surfaced findings were unchanged), the sweep is done immediately.

    Idempotent: atomically claims the researching→reviewing transition so a
    double-fire (concurrent/duplicate producer submit, or a steward review_spawn)
    only fans out once. Called by the autonomous path AND the steward's
    review_spawn for interactive sweeps."""
    pool = get_pool()
    claimed = await pool.fetchval(
        "UPDATE platform_sweeps SET status='reviewing' WHERE id=$1 AND status='researching' "
        "RETURNING id", run_id)
    if not claimed:
        return  # another caller already began (or the sweep is no longer researching)
    pending = await _run_pending(run_id)
    if not pending:
        await _finish_sweep(run_id)
        return
    # One reviewer per (research × SOURCE) group: all of a research's findings are
    # reviewed by a single worker, but model_research vs tool_research findings are
    # reviewed by SEPARATE workers.
    by_source: dict[str, list] = {}
    for f in pending:
        by_source.setdefault(f.get("source") or "unknown", []).append(f)
    log.info("platform: sweep %s reviewing — %d findings across %d source-group(s)",
             run_id, len(pending), len(by_source))
    for source, group in by_source.items():
        kind = _SOURCE_TO_KIND.get(source, source)
        label = f"{run_id[:8]}-{kind}"
        try:
            res = await _worker.spawn_reviewer(group, label)
            if not res.get("ok"):
                log.warning("platform: reviewer spawn failed for %s (%d findings): %s",
                            label, len(group), res.get("reason"))
        except Exception:
            log.exception("platform: reviewer spawn errored for %s", label)


async def on_verdict(finding_id: str) -> None:
    """Called after a reviewer's registry_verdict. When the sweep has no findings
    left pending, it is done."""
    pool = get_pool()
    run_id = await pool.fetchval("SELECT run_id FROM platform_registry WHERE id=$1", finding_id)
    if not run_id:
        return
    sweep = await pool.fetchrow(
        "SELECT id, status FROM platform_sweeps WHERE id=$1 AND status='reviewing'", run_id)
    if not sweep:
        return
    remaining = await pool.fetchval(
        "SELECT COUNT(*) FROM platform_registry WHERE run_id=$1 AND verdict='pending'", run_id)
    if int(remaining or 0) == 0:
        await _finish_sweep(run_id)


async def _finish_sweep(run_id: str) -> None:
    """Mark a sweep done and wake the steward to narrate the digest. Idempotent:
    only the caller that actually flips the status proceeds, so concurrent final
    verdicts don't double-narrate."""
    pool = get_pool()
    row = await pool.fetchrow(
        "UPDATE platform_sweeps SET status='done', finished_at=NOW() "
        "WHERE id=$1 AND status IN ('researching','reviewing') RETURNING thread_id", run_id)
    if not row:
        return  # already finished by a concurrent caller
    log.info("platform: sweep %s done — waking steward to narrate", run_id)
    # Wake the research drainer so it reconciles the linked discovery (-> reviewed)
    # and claims the next queued one without waiting for its poll interval.
    try:
        await pool.execute("SELECT pg_notify('discovery_wake', 'sweep_done')")
    except Exception:
        pass
    await _wake_steward(row["thread_id"], await _sweep_digest_content(run_id))


# ── Steward narration (SYSTEM_WAKE → its own turn in the platform goal chat) ──

async def _wake_steward(thread_id: str | None, content: str) -> None:
    """Wake the steward to post into the platform goal chat as its OWN queue turn
    (SYSTEM_WAKE serializes under the per-thread advisory lock). Falls back to the
    GO system-message path if the durable queue is disabled."""
    if not thread_id:
        log.warning("platform: no thread_id — cannot wake steward")
        return
    try:
        from app.chat_module.system_ingress import enqueue_system_wake
        mid = await enqueue_system_wake(
            thread_id=thread_id, goal_id=PLATFORM_GOAL_ID, agent_id=STEWARD_AGENT_ID,
            gateway_agent_id=STEWARD_AGENT_ID, agent_type="Platform", content=content)
        if mid:
            return
    except Exception:
        log.exception("platform: enqueue_system_wake failed — falling back")
    try:
        from app.services.go_events import _send_system_message_to_go
        # mirror_to_mesh=False: this is a fallback for the primary enqueue_system_wake
        # path above, which never mirrors to Agent Mesh either — content here is the
        # steward's own narration script, not a human-facing event notice.
        await _send_system_message_to_go(
            PLATFORM_GOAL_ID, content, wake_up=True, mirror_to_mesh=False,
        )
    except Exception:
        log.exception("platform: steward wake fallback failed")


async def _sweep_digest_content(run_id: str) -> str:
    """Build the [System] digest the steward turns into an admin-facing summary."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT category, target_role, title, summary, verdict, adoption_path, supported_today "
        "FROM platform_registry WHERE run_id=$1 ORDER BY verdict, category", run_id)
    if not rows:
        return ("[System] A research sweep just finished but recorded no new findings. "
                "Let the admins know briefly, and don't re-run research.")
    lines = []
    for r in rows:
        st = "" if r["supported_today"] is None else (
            " (supported today)" if r["supported_today"] else " (NOT supported today)")
        lines.append(
            f"- [{r['verdict']}{st}] {r['category']}/{r['target_role']}: {r['title']} — "
            f"{r['summary']}  · adoption: {r['adoption_path'] or 'n/a'}")
    return (
        "[System] A research sweep just finished. Post a concise digest to the admins in "
        "this thread: lead with what's newly adoptable WITHOUT a redeploy (adopt_now), then "
        "what needs a build (adopt_with_build), then note anything rejected/not-applicable in "
        "one line. Offer to create stories for the adoptable items. Use registry_get for full "
        "detail if needed; do NOT re-run research. Findings + verdicts:\n\n"
        + "\n".join(lines[:60]))


# ── Nudge watchdog (re-prompt stalled workers to call their finish tool) ─────

NUDGE_MAX = 3            # at most N nudges per sweep before the stale-fail backstop
NUDGE_SPACING_S = 120    # ~2 min between nudges


async def nudge_stalled_sweeps() -> int:
    """Re-prompt producers/reviewers in an in-flight sweep that have gone quiet
    without calling their finish tool. Spaced ~NUDGE_SPACING_S apart (the Nth nudge
    fires only after created_at + N*spacing), capped at NUDGE_MAX per sweep; after
    that the stale-fail backstop (active_sweep) clears it. Returns nudges sent."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, status, kinds, producers_done, nudges FROM platform_sweeps "
        "WHERE status IN ('researching','reviewing') AND nudges < $1 "
        "AND EXTRACT(EPOCH FROM (NOW() - created_at)) > (nudges + 1) * $2",
        NUDGE_MAX, NUDGE_SPACING_S,
    )
    sent = 0
    for s in rows:
        attempt = s["nudges"] + 1
        if s["status"] == "researching":
            for kind in [k for k in (s["kinds"] or []) if k not in (s["producers_done"] or [])]:
                if await _worker.nudge_producer(kind, s["id"], attempt):
                    sent += 1
        else:  # reviewing — nudge the reviewer of each source-group with findings still pending
            pending = await _run_pending(s["id"])
            for source in {f.get("source") for f in pending}:
                kind = _SOURCE_TO_KIND.get(source, source or "x")
                if await _worker.nudge_reviewer(f"{s['id'][:8]}-{kind}", attempt):
                    sent += 1
        await pool.execute("UPDATE platform_sweeps SET nudges=nudges+1 WHERE id=$1", s["id"])
    if sent:
        log.info("platform: nudge watchdog sent %d re-prompt(s) to stalled workers", sent)
    return sent


# ── Event-driven review backstop ─────────────────────────────────────────────

async def review_orphan_pending(min_age_seconds: int = 300, limit: int = 50) -> int:
    """Independently review findings left `verdict='pending'` with no live reviewer
    — the event-driven safety net behind the sweep fan-out. Catches findings from a
    failed/abandoned sweep, ad-hoc writes, or a reviewer that died. min_age_seconds
    gives the sweep's own fan-out time to act first.

    Groups orphans by (run_id, source) and spawns ONE reviewer per group (matching
    the sweep fan-out: a research's findings reviewed together, model vs tool by
    separate workers). Returns the number of reviewer groups spawned.
    """
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, run_id, source FROM platform_registry WHERE verdict='pending' "
        "AND updated_at < NOW() - ($1 || ' seconds')::interval "
        "ORDER BY run_id, source, updated_at LIMIT $2",
        str(int(min_age_seconds)), int(limit),
    )
    # Group by (run_id, source). NULL run_id (truly ad-hoc) keys on the finding id
    # so each is its own group.
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["run_id"] or f"adhoc:{r['id']}", r["source"] or "unknown")
        groups.setdefault(key, []).append(r["id"])

    spawned = 0
    for (run_key, source), fids in groups.items():
        kind = _SOURCE_TO_KIND.get(source, source)
        run8 = run_key[:8] if not run_key.startswith("adhoc:") else run_key[6:14]
        label = f"{run8}-{kind}"
        if await pool.fetchval(
                "SELECT 1 FROM agents WHERE id=$1 AND status='active'",
                _worker.reviewer_instance_id(label)):
            continue  # a reviewer is already on this group
        findings = [f for f in [await _registry.get(fid) for fid in fids] if f]
        if not findings:
            continue
        try:
            res = await _worker.spawn_reviewer(findings, label)
            if res.get("ok"):
                spawned += 1
            else:
                log.warning("platform: backstop reviewer spawn failed for %s: %s",
                            label, res.get("reason"))
        except Exception:
            log.exception("platform: backstop reviewer spawn errored for %s", label)
    if spawned:
        log.info("platform: review backstop spawned %d reviewer group(s) for orphan-pending", spawned)
    return spawned
