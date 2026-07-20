"""Goal Summary scheduler — periodically dispatches the GO for every active
goal that hasn't had its summary updated within the configured interval.

Design notes
------------
* Due check is per-goal, not global: a goal is due when its
  `goal_summaries.requested_at < now() - interval '${interval_minutes} minutes'`
  OR has no row yet. This means each goal's cadence is independent.
* `requested_at` is written provisionally by the dispatcher BEFORE the GO
  session is woken. This prevents re-dispatch within the interval even while
  the GO is still generating. If the GO never calls the tool (e.g. session
  cold), the row ages out after the interval and is retried.
* The loop ticks every `tick_seconds` (well under the interval) and fires
  only the goals that are due. No advisory locks needed — provisional
  requested_at write is the dedup mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import os

from app.database import get_pool

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class GoalSummaryScheduler:
    """Background task that dispatches the GO for every active goal that is
    due for a summary update (default every 25 minutes)."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._enabled = _env_bool("GOAL_SUMMARY_ENABLED", True)
        self._interval_minutes = _env_int("GOAL_SUMMARY_INTERVAL_MINUTES", 25)
        self._tick_seconds = _env_int("GOAL_SUMMARY_TICK_SECONDS", 120)

    def start(self) -> None:
        if not self._enabled:
            log.info("goal-summary scheduler disabled (GOAL_SUMMARY_ENABLED=false)")
            return
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())
            log.info(
                "goal-summary scheduler started — interval=%dm tick=%ds",
                self._interval_minutes, self._tick_seconds,
            )

    async def stop(self) -> None:
        log.info("goal-summary scheduler stopping")
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        log.info("goal-summary scheduler stopped")

    async def _run_forever(self) -> None:
        log.info("goal-summary scheduler: waiting 60s for startup to settle")
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        log.info("goal-summary scheduler: entering tick loop")
        while not self._stop.is_set():
            try:
                await self._maybe_dispatch_batch()
            except Exception as exc:
                log.exception("goal-summary scheduler tick error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _maybe_dispatch_batch(self) -> None:
        pool = get_pool()

        # Count total active goals to distinguish "none active" from "all up to date".
        total_active = await pool.fetchval(
            "SELECT COUNT(*) FROM goals WHERE status = 'active' "
            "AND go_agent_id IS NOT NULL AND go_thread_id IS NOT NULL",
        )
        log.debug("goal-summary tick: %d active goal(s) with GO wiring", total_active)

        if not total_active:
            log.debug("goal-summary tick: no active goals with GO wiring — nothing to do")
            return

        # Active goals whose documents are due AND have had activity since last generation.
        # Uses the 'code' doc row in goal_documents as the interval reference — both
        # doc types are dispatched together so checking one is sufficient.
        # Two conditions must both be true:
        #   1. Interval passed — requested_at age check on the 'code' doc row.
        #   2. Activity since last generation — at least one ledger event newer than
        #      generated_at, excluding summary-bookkeeping events.
        goals_due = await pool.fetch(
            """
            SELECT g.id, g.name
              FROM goals g
             WHERE g.status = 'active'
               AND g.go_agent_id IS NOT NULL
               AND g.go_thread_id IS NOT NULL
               AND (
                     -- No doc row yet → always due (first generation)
                     NOT EXISTS (
                         SELECT 1 FROM goal_documents gd
                          WHERE gd.goal_id = g.id AND gd.doc_type = 'code'
                     )
                     OR EXISTS (
                         SELECT 1 FROM goal_documents gd
                          WHERE gd.goal_id = g.id AND gd.doc_type = 'code'
                            -- Condition 1: interval has elapsed
                            AND (gd.requested_at IS NULL
                                 OR gd.requested_at < now() - make_interval(mins => $1))
                            -- Condition 2: a story transitioned to 'done' or 'blocked'
                            -- since the last generation. These are the only events
                            -- significant enough to warrant a doc refresh.
                            AND (
                              gd.generated_at IS NULL
                              OR EXISTS (
                                SELECT 1 FROM ledger_events le
                                 WHERE le.context_goal_id = g.id
                                   AND le.event_type = 'story.state_changed'
                                   AND le.timestamp > gd.generated_at
                                   AND le.payload->>'to_state' IN ('done', 'blocked')
                                 LIMIT 1
                              )
                            )
                     )
                   )
             ORDER BY g.created_at
            """,
            self._interval_minutes,
        )

        if not goals_due:
            log.debug(
                "goal-summary tick: all %d active goal(s) are up to date or idle — skipping",
                total_active,
            )
            return

        log.info(
            "goal-summary tick: %d/%d goal(s) due with activity since last generation",
            len(goals_due), total_active,
        )
        for g in goals_due:
            log.debug("goal-summary tick: due — goal %s (%s)", g["id"], g["name"])

        from app.services.goal_summary.dispatcher import dispatch_summary_for_goal

        dispatched = skipped = errors = 0
        for g in goals_due:
            try:
                ok = await dispatch_summary_for_goal(g["id"])
                if ok:
                    dispatched += 1
                else:
                    skipped += 1
                    log.info(
                        "goal-summary tick: skipped goal %s (%s) — no GO agent",
                        g["id"], g["name"],
                    )
            except Exception:
                errors += 1
                log.exception(
                    "goal-summary tick: dispatch error for goal %s (%s)",
                    g["id"], g["name"],
                )

        log.info(
            "goal-summary tick done: %d due → %d dispatched, %d skipped, %d errors",
            len(goals_due), dispatched, skipped, errors,
        )


goal_summary_scheduler = GoalSummaryScheduler()
