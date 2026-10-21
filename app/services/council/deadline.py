"""The failure a council run cannot report on itself.

A STAGE THAT WEDGES IS NEITHER PROGRESSING NOR TERMINAL. The reviewer's
own nudge loop covers the case where Flash ends a turn without calling
its finish tool, and `_safe_run_stage` covers a crash. Neither covers the
gateway agent that accepted the run and never answered: no status is ever
written, so the run sits `running` for ever, the global single-run index
means NO OTHER GOAL CAN BE REVIEWED while it does, and the twelve-hour
scheduler quietly stops working everywhere.

That last part is why this exists at all. A wedged story review costs one
story; a wedged council run costs every goal.

IT DELIVERS, IT DOES NOT JUST CLEAN UP. Ending the run without a packet
would satisfy the index and fail the requirement -- every goal review
produces its findings. Whatever the stages that did run recorded is
already in the ledger, so the sweep draws a packet from it and sends it,
then closes the run.

ALSO REAPS THE GATEWAY AGENT. `run_stage` deletes its RA in a `finally`,
which does not run when the process holding it died. A wedged stage
therefore leaks a reviewer and its workspace, and nothing else would ever
collect them.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# A stage is one Flash turn over documents, with a nudge loop of roughly
# (1 + 3) * 90s behind it. Forty minutes is far past a slow-but-working
# reviewer and well inside the twelve-hour cycle, so a wedge never
# survives to block the next scheduled run.
DEFAULT_MINUTES = 40
_ENV = "AGENTOS_COUNCIL_DEADLINE_MINUTES"


def deadline_minutes() -> int:
    """Read per call, so a wedge can be shortened without a redeploy."""
    if _ENV not in os.environ:
        return DEFAULT_MINUTES
    try:
        return max(1, int(os.environ[_ENV]))
    except ValueError:
        logger.warning("%s is not a number; using %d", _ENV, DEFAULT_MINUTES)
        return DEFAULT_MINUTES


async def _overdue(conn, minutes: int) -> list:
    """Runs whose last stage movement is past the deadline."""
    return list(await conn.fetch(
        """
        SELECT r.id, r.goal_id
          FROM council_review_runs r
         WHERE r.status = 'running'
           AND r.created_at < now() - ($1 || ' minutes')::interval
           AND NOT EXISTS (
                 SELECT 1 FROM council_review_stages s
                  WHERE s.run_id = r.id
                    AND s.finished_at > now() - ($1 || ' minutes')::interval)
         ORDER BY r.created_at
         LIMIT 10
        """, str(minutes)))


async def _reap_agents(conn, run_id: str) -> int:
    """Delete gateway reviewers whose stage never reached a terminal state."""
    from app.openclaw import gateway_admin

    rows = await conn.fetch(
        "SELECT instance_id FROM council_review_stages "
        "WHERE run_id = $1 AND instance_id IS NOT NULL "
        "AND status NOT IN ('done', 'failed')", run_id)
    reaped = 0
    for row in rows:
        try:
            await gateway_admin.delete_agent(row["instance_id"], delete_files=True)
            reaped += 1
        except Exception as exc:                            # noqa: BLE001
            # Already gone is the outcome we wanted.
            if "not found" not in str(exc).lower():
                logger.warning("council sweep: could not delete %s: %s",
                               row["instance_id"], exc)
    return reaped


async def sweep(conn, *, dry_run: bool = False) -> list[dict]:
    """End wedged runs, deliver what they found, free the single-run slot.

    Never raises into the tick: this runs alongside the dispatcher's own
    loop, and a sweep that dies takes the scheduler with it.
    """
    from app.services.council.deliver import deliver_ledger_packet

    minutes = deadline_minutes()
    actions: list[dict] = []

    for row in await _overdue(conn, minutes):
        run_id, goal_id = row["id"], row["goal_id"]
        actions.append({"run_id": run_id, "goal": goal_id,
                        "action": "council_deadline",
                        "detail": f"no stage finished in {minutes}m"})
        if dry_run:
            continue
        try:
            await conn.execute(
                "UPDATE council_review_stages SET status = 'failed', "
                "error = COALESCE(NULLIF(error, ''), $2), finished_at = now() "
                "WHERE run_id = $1 AND status NOT IN ('done', 'failed')",
                run_id, f"the council deadline of {minutes} minutes passed "
                        f"with no stage finishing")
            reaped = await _reap_agents(conn, run_id)
            # THE PACKET FIRST, THEN THE RUN CLOSES. Closing first would
            # free the single-run slot a moment earlier and risk the next
            # run's findings interleaving with this one's delivery.
            await deliver_ledger_packet(goal_id, run_id)
            await conn.execute(
                "UPDATE council_review_runs SET status = 'failed', "
                "finished_at = now() WHERE id = $1 AND status = 'running'",
                run_id)
            logger.warning("council sweep: run %s (goal %s) timed out after "
                           "%dm; %d reviewer(s) reaped, ledger packet sent",
                           run_id, goal_id, minutes, reaped)
        except Exception:                                   # noqa: BLE001
            logger.exception("council sweep: run %s", run_id)

    return actions
