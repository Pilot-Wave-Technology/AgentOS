"""Council scheduler — periodically enqueues a full Goal Review for every
active goal.

This complements (does NOT replace) the other two ways a council run starts:
  - the manual run endpoint (`/goals/{pid}/council/run`), and
  - the event-driven `story_done` story review.

Design notes
------------
* The schedule ledger is `council_review_runs` itself: a batch is "due" when no
  run with `trigger='scheduled'` was created within the last `interval_hours`.
  No extra table, and it survives process restarts (a redeploy resets the
  in-memory tick timer but NOT the due-check, so cadence doesn't drift or
  double-fire across restarts).
* The loop ticks every `tick_seconds` (well under the 12h window) and fires the
  batch the first time it observes the window has elapsed.
* `enqueue_run` already dedups per goal (one pending/running run at a time), so
  if two ticks/instances race, or a goal already has a manual run queued, each
  goal still gets at most one run — no advisory lock needed.
* Global single-run is DB-enforced in the dispatcher, so the fanned-out batch
  drains one goal at a time; enqueue is cheap and just fills the queue.
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


class CouncilScheduler:
    """Background task that enqueues a council run for every active goal once
    per `interval_hours`."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._enabled = _env_bool("COUNCIL_SCHEDULE_ENABLED", True)
        self._interval_hours = _env_int("COUNCIL_SCHEDULE_HOURS", 12)
        self._tick_seconds = _env_int("COUNCIL_SCHEDULE_TICK_SECONDS", 600)

    def start(self) -> None:
        if not self._enabled:
            log.info("council scheduler disabled (COUNCIL_SCHEDULE_ENABLED=false)")
            return
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())
            log.info(
                "council scheduler started — every %dh for active goals (tick %ds)",
                self._interval_hours, self._tick_seconds,
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run_forever(self) -> None:
        # Let startup (DB pool, dispatcher, LISTEN) settle before the first check.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self._maybe_run_batch()
            except Exception as exc:  # never let the loop die
                log.exception("council scheduler tick error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _maybe_run_batch(self) -> None:
        pool = get_pool()

        # Due iff no scheduled run was created within the last interval (DB clock).
        due = await pool.fetchval(
            "SELECT NOT EXISTS ("
            "  SELECT 1 FROM council_review_runs "
            "  WHERE trigger = 'scheduled' "
            "    AND created_at > now() - make_interval(hours => $1))",
            self._interval_hours,
        )
        if not due:
            return

        # Active goals that actually have a GO to receive the review.
        goals = await pool.fetch(
            "SELECT id FROM goals "
            "WHERE status = 'active' "
            "  AND go_agent_id IS NOT NULL "
            "  AND go_thread_id IS NOT NULL "
            "ORDER BY created_at",
        )
        if not goals:
            log.info("council scheduler: batch due but no active goals with GO wiring")
            return

        from app.services.council.runs import enqueue_run

        created = skipped = 0
        for g in goals:
            try:
                res = await enqueue_run(
                    g["id"], created_by="council-scheduler", trigger="scheduled",
                )
                if res.get("created"):
                    created += 1
                else:
                    skipped += 1  # already had a pending/running run
            except Exception:
                log.exception("council scheduler: enqueue failed for goal %s", g["id"])
        log.info(
            "council scheduler: 12h batch — %d active goal(s), %d enqueued, %d already queued",
            len(goals), created, skipped,
        )


council_scheduler = CouncilScheduler()
