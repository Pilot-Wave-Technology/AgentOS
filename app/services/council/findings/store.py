"""Writing findings to the ledger, and recording what happened to them.

WRITTEN AS EACH STAGE FINISHES, not once at the end of the run. A crash
in the convener must not lose what `progress` and `goal_health` already
found -- that is half of "every goal review produces its findings", and
the cheap half. The other half is `render`, which can draw a packet from
the ledger when no convener ever runs.

REOPENING KEEPS THE ROW. A finding that was resolved and comes back
updates the same (goal, key) rather than starting a new one, so
`times_seen` counts recurrences across the whole life of the goal. How
many times something has come back is worth more than a tidy list of
distinct rows, and it is the number the escalation ladder reads.
"""

from __future__ import annotations

import logging
import uuid

from app.database import get_pool

logger = logging.getLogger(__name__)


async def _event(conn, finding_id: str, run_id: str, kind: str,
                 detail: str = "") -> None:
    await conn.execute(
        "INSERT INTO council_finding_events (id, finding_id, run_id, kind, detail) "
        "VALUES ($1, $2, $3, $4, $5)",
        uuid.uuid4().hex, finding_id, run_id or "", kind, (detail or "")[:4000])


async def record(goal_id: str, run_id: str, items: list[dict]) -> int:
    """Upsert one stage's findings. Returns how many rows were touched.

    Never raises into the dispatcher: a stage that reviewed well and
    whose findings failed to store should still count as a stage that
    ran, and the packet is drawn from whatever did land.
    """
    if not items:
        return 0
    pool = get_pool()
    touched = 0
    for item in items:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO council_findings
                        (id, goal_id, key, scope, story_id, title, body,
                         severity, raised_by, first_run_id, last_run_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10)
                    ON CONFLICT (goal_id, key) DO UPDATE SET
                        -- REOPENED, and the count keeps running. A finding
                        -- that was resolved and recurred is a stronger
                        -- signal than one raised for the first time.
                        status      = 'open',
                        title       = EXCLUDED.title,
                        body        = EXCLUDED.body,
                        severity    = EXCLUDED.severity,
                        scope       = EXCLUDED.scope,
                        story_id    = EXCLUDED.story_id,
                        raised_by   = EXCLUDED.raised_by,
                        last_run_id = EXCLUDED.last_run_id,
                        times_seen  = council_findings.times_seen
                                      + (CASE WHEN council_findings.last_run_id
                                                   = EXCLUDED.last_run_id
                                              THEN 0 ELSE 1 END),
                        updated_at  = now()
                    RETURNING id, times_seen
                    """,
                    uuid.uuid4().hex, goal_id, item["key"], item["scope"],
                    item.get("story_id"), item["title"], item.get("body", ""),
                    item["severity"], item.get("raised_by", ""), run_id)
                await _event(
                    conn, row["id"], run_id,
                    "raised" if row["times_seen"] == 1 else "reaffirmed",
                    item["title"])
                touched += 1
        except Exception:                                   # noqa: BLE001
            logger.exception("council findings: could not record %s/%s",
                             goal_id, item.get("key"))
    return touched


async def apply_accounting(goal_id: str, run_id: str,
                           verdicts: dict[str, tuple[str, str]]) -> int:
    """Close the findings a reviewer explicitly said were done with.

    ONLY THE ONES IT NAMED. A key absent from `verdicts` stays open --
    the reviewer not mentioning it means it did not look, and inferring
    "fixed" from silence is how a real problem disappears from a review
    that nobody noticed had stopped covering it.
    """
    if not verdicts:
        return 0
    pool = get_pool()
    closed = 0
    for key, (status, detail) in verdicts.items():
        if status == "open":
            continue
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE council_findings SET status = $3, updated_at = now() "
                    "WHERE goal_id = $1 AND key = $2 AND status = 'open' "
                    "RETURNING id",
                    goal_id, key, status)
                if row:
                    await _event(conn, row["id"], run_id, "resolved",
                                 f"{status}: {detail}")
                    closed += 1
        except Exception:                                   # noqa: BLE001
            logger.exception("council findings: could not close %s/%s",
                             goal_id, key)
    return closed


async def open_findings(goal_id: str, limit: int = 200) -> list[dict]:
    """Open findings with the ladder attached, worst and oldest first."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT f.id, f.key, f.scope, f.story_id, f.title, f.body,
               f.severity, f.raised_by, f.times_seen, f.created_at,
               (SELECT e.detail FROM council_finding_events e
                 WHERE e.finding_id = f.id AND e.kind = 'go_response'
                 ORDER BY e.created_at DESC LIMIT 1) AS go_said
          FROM council_findings f
         WHERE f.goal_id = $1 AND f.status = 'open'
         ORDER BY CASE f.severity WHEN 'high' THEN 0
                                  WHEN 'medium' THEN 1 ELSE 2 END,
                  f.times_seen DESC, f.created_at
         LIMIT $2
        """, goal_id, limit)
    return [dict(r) for r in rows]


async def record_go_response(goal_id: str, key: str, text: str,
                             run_id: str = "") -> bool:
    """Attach the GO's answer to the finding it cited."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id FROM council_findings WHERE goal_id = $1 AND key = $2",
        goal_id, key)
    if not row:
        return False
    async with pool.acquire() as conn:
        await _event(conn, row["id"], run_id, "go_response", text)
    return True
