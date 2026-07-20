"""Goal-weightage recompute watchdog.

Checks periodically whether any active goal's story/resource activity has
grown since its `weightage` was last recomputed, and if so, pokes the
`task-runner` Cloud Run Job to pick up the work. See
`D:\\Projects\\agent-control-panel\\TAXIMETER_V5_REDESIGN.md` for the full
design discussion.

Deliberately minimal: no queue, no state beyond `goals.weightage_recomputed_at`
itself. Runs on the `agent-management` service (`app/management_worker.py`),
the same shape as `app/main.py`'s SHCA cleanup watchdog — a plain asyncio
loop started at boot on a fixed tick.

The actual recompute (Gemini Flash call, `goals.weightage` update) happens
in `task_runner/tasks/goal_weightage_recompute.py`, a completely separate
deployable — this watchdog only ever checks "is there stale work" and
pokes the job; it never calls Gemini or writes to `goals` itself.
"""
import asyncio
import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

# Weightage is a slow-moving signal — goal scope doesn't change minute to
# minute — so this doesn't need to be frequent. 2 hours balances staying
# reasonably current against spamming the Cloud Run Jobs API for a feature
# with no real-time urgency.
_CHECK_INTERVAL_SECONDS = 2 * 60 * 60

# A goal is "stale" if it's never been recomputed, or if a story/resource
# was created after its last recompute — the concrete definition of
# "scope has grown since we last looked."
_STALE_GOALS_QUERY = """
    SELECT count(*) FROM goals g
     WHERE g.status = 'active'
       AND (
             g.weightage_recomputed_at IS NULL
          OR EXISTS (
               SELECT 1 FROM stories s
                WHERE s.goal_id = g.id AND s.created_at > g.weightage_recomputed_at
             )
          OR EXISTS (
               SELECT 1 FROM goal_resources gr
                WHERE gr.goal_id = g.id AND gr.created_at > g.weightage_recomputed_at
             )
           )
"""


async def weightage_watchdog_loop() -> None:
    """Runs forever: every `_CHECK_INTERVAL_SECONDS`, check for goals whose
    weightage is stale and trigger task-runner if any exist. Errors are
    non-fatal — a missed check just means the next tick tries again."""
    from app.services.task_runner_trigger import trigger_task_runner  # noqa: PLC0415

    _log.info(
        "weightage watchdog started (check interval=%ds)", _CHECK_INTERVAL_SECONDS,
    )
    await asyncio.sleep(60)  # let the DB pool finish initializing at boot
    while True:
        try:
            pool = get_pool()
            stale_count = await pool.fetchval(_STALE_GOALS_QUERY)
            if stale_count:
                _log.info(
                    "weightage watchdog: %d goal(s) need a weightage recompute — "
                    "triggering task-runner",
                    stale_count,
                )
                await trigger_task_runner("weightage-watchdog")
            else:
                _log.debug("weightage watchdog: no stale goals this tick")
        except Exception:
            _log.exception("weightage watchdog: check failed — will retry next tick")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
