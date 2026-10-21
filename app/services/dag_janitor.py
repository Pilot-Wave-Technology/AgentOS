"""DAG janitor — periodic backstop for claude-code phase DAGs.

Recovers two otherwise-silent PERMANENT stalls that today require a human to
notice and hand-complete a task:

1. Missed rollup — a `claude-code-dag` parent stuck `in_progress` while ALL its
   `claude-code-phase` children are terminal (a missed/failed rollup trigger,
   e.g. the swallowed `_maybe_finish_dag` ImportError in check_parent_completion).
   Re-runs the idempotent finisher (`_on_dag_tasks_complete`) so the diff-review
   card posts / the DAG completes.

2. Stalled subtask — a `claude-code-phase` task `in_progress` with no live turn:
   the invoke returned without a parseable sentinel (off-protocol), or its result
   was lost. The turn's hard timeout is 30 min (phase_dispatcher invoke
   timeout=1800), so a task `in_progress` past STALL_THRESHOLD_MINUTES has no live
   turn. Re-dispatch it (same Claude session, full prior context, so it re-emits
   its sentinel); the C3 per-story lock guards against a double turn if the
   original is somehow still live (spawnCliDirect returns busy → the phase
   dispatcher reverts to ready and retries). After MAX_STALL_REDISPATCH tries it
   is failed so the DAG cascade surfaces to the human instead of looping forever.

All actions are CAS-guarded / idempotent and best-effort — a janitor error never
crashes the loop, and it never touches tasks with a live turn.
"""
from __future__ import annotations

import asyncio
import logging

from app.database import get_pool
from app.ledger.journal import append_event
from app.services.sentinel_parser import SYSTEM_ACTOR_ID

_log = logging.getLogger("app.dag_janitor")

SWEEP_INTERVAL_SECONDS = 300      # 5 min
STALL_THRESHOLD_MINUTES = 40      # > the 30-min invoke hard timeout + buffer
MAX_STALL_REDISPATCH = 2          # re-dispatch this many times, then fail + cascade


async def run_loop() -> None:
    _log.info("dag_janitor loop started (%ds interval)", SWEEP_INTERVAL_SECONDS)
    await asyncio.sleep(90)  # let boot + the first dispatches settle
    while True:
        try:
            await _sweep_once()
        except Exception:
            _log.exception("dag_janitor sweep failed — non-fatal, retry next cycle")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def _sweep_once() -> None:
    pool = get_pool()
    # Recover stalled subtasks first — a re-dispatch/escalation may make a DAG's
    # subtasks all-terminal, which the rollup pass then picks up.
    await _recover_stalled_subtasks(pool)
    await _rollup_stuck_dags(pool)


async def _rollup_stuck_dags(pool) -> None:
    """claude-code-dag parents stuck in_progress whose phase children are ALL
    terminal → re-run the idempotent finisher (posts the diff-review card /
    completes the DAG)."""
    from app.services.task_states import TERMINAL_PHASE_STATES  # noqa: PLC0415
    rows = await pool.fetch(
        """
        SELECT p.id AS dag_id, p.goal_id, p.story_id
          FROM tasks p
         WHERE p.executor = 'claude-code-dag'
           AND p.state = 'in_progress'
           -- Deleted-goal guard (goal delete is soft + doesn't cascade to tasks).
           AND EXISTS (
                 SELECT 1 FROM goals g
                  WHERE g.id = p.goal_id AND g.status = 'active')
           AND EXISTS (
                 SELECT 1 FROM tasks c
                  WHERE c.parent_task_id = p.id
                    AND c.executor = 'claude-code-phase')
           AND NOT EXISTS (
                 SELECT 1 FROM tasks c
                  WHERE c.parent_task_id = p.id
                    AND c.executor = 'claude-code-phase'
                    AND c.state <> ALL($1))
        """,
        list(TERMINAL_PHASE_STATES),
    )
    for r in rows:
        try:
            from app.services.sentinel_parser import _on_dag_tasks_complete  # noqa: PLC0415
            await _on_dag_tasks_complete(r["goal_id"], r["story_id"], r["dag_id"])
            _log.info(
                "dag_janitor: rolled up stuck DAG %s (all subtasks terminal, story=%s)",
                r["dag_id"], r["story_id"],
            )
        except Exception:
            _log.exception("dag_janitor: rollup failed for DAG %s", r["dag_id"])


async def _recover_stalled_subtasks(pool) -> None:
    """claude-code-phase tasks in_progress past the turn timeout with no live
    turn → re-dispatch (bounded by a DURABLE per-task counter), then fail +
    cascade. If the story has no live claude session, re-dispatch is futile
    (dispatch_phase_task early-returns on an empty/__prespawn_ session), so
    escalate directly instead of looping."""
    rows = await pool.fetch(
        f"""
        SELECT t.id, t.goal_id, t.story_id, t.parent_task_id, t.phase,
               t.stall_redispatch_count AS attempts,
               s.current_claude_session_id AS session
          FROM tasks t
          JOIN stories s ON s.id = t.story_id
         WHERE t.executor = 'claude-code-phase'
           AND t.state = 'in_progress'
           AND t.updated_at < now() - interval '{STALL_THRESHOLD_MINUTES} minutes'
           -- Deleted-goal guard (goal delete is soft + doesn't cascade to tasks).
           AND EXISTS (
               SELECT 1 FROM goals g
                WHERE g.id = t.goal_id AND g.status = 'active'
           )
           -- NEVER reap LOCAL stories: a local phase runs in the human's Claude Code turn
           -- (which has no 30-min cap), and its session id is stored as '' so the
           -- session-dead heuristic below would wrongly hard-fail a LIVE task and cascade
           -- the DAG. Local stall recovery is human-driven (extension Retry/Replan, C6).
           AND s.execution_mode IS DISTINCT FROM 'local'
        """,
    )
    for r in rows:
        try:
            attempts = int(r["attempts"] or 0)
            session = (r["session"] or "").strip()
            session_dead = (not session) or session.startswith("__prespawn_")
            if attempts >= MAX_STALL_REDISPATCH or session_dead:
                await _escalate_stalled(pool, r, attempts, session_dead)
            else:
                await _redispatch_stalled(pool, r, attempts)
        except Exception:
            _log.exception("dag_janitor: stall recovery failed for task %s", r["id"])


async def _redispatch_stalled(pool, task_row, prior: int) -> None:
    task_id = task_row["id"]
    # CAS + durable increment in ONE statement: only revert if STILL in_progress
    # (never clobber a late transition — e.g. a delayed sentinel result that just
    # landed), and bump stall_redispatch_count atomically so the retry cap can't
    # be defeated by a failed ledger write.
    new_count = await pool.fetchval(
        "UPDATE tasks SET state = 'ready', "
        "  stall_redispatch_count = stall_redispatch_count + 1, "
        "  updated_at = now() "
        "WHERE id = $1 AND state = 'in_progress' "
        "RETURNING stall_redispatch_count",
        task_id,
    )
    if new_count is None:
        return
    await _emit(task_row, "phase_task.stall_redispatch",
               {"attempt": new_count, "phase": task_row.get("phase")})
    _log.warning(
        "dag_janitor: re-dispatching stalled phase task %s (phase=%s, attempt %d/%d)",
        task_id, task_row.get("phase"), new_count, MAX_STALL_REDISPATCH,
    )
    # Wake the controller so it re-dispatches this now-ready task next tick.
    try:
        from app.task_controller import trigger as tc_trigger  # noqa: PLC0415
        tc_trigger()
    except Exception:
        pass


async def _escalate_stalled(pool, task_row, prior: int, session_dead: bool = False) -> None:
    task_id = task_row["id"]
    if session_dead:
        reason = (
            f"phase stalled in_progress past {STALL_THRESHOLD_MINUTES}m with no live "
            f"claude session for the story — re-dispatch is impossible; failing so the "
            f"DAG surfaces to a human (reset/re-import the story to re-provision a session)"
        )
    else:
        reason = (
            f"phase stalled with no sentinel after {prior} re-dispatch(es) "
            f"(no live turn past {STALL_THRESHOLD_MINUTES}m) — failing so the DAG "
            f"surfaces to a human"
        )
    from app.services.sentinel_parser import _fail_dag_cascade, _mark_task_failed  # noqa: PLC0415
    await _mark_task_failed(task_id, reason)
    await _emit(task_row, "phase_task.stall_failed",
                {"attempts": prior, "phase": task_row.get("phase")})
    _log.error(
        "dag_janitor: escalating stalled phase task %s to FAILED after %d re-dispatches",
        task_id, prior,
    )
    try:
        await _fail_dag_cascade(
            task_row["goal_id"], task_row["story_id"],
            task_row.get("parent_task_id"), task_id, reason,
        )
    except Exception:
        _log.exception("dag_janitor: fail cascade failed for %s", task_id)


async def _emit(task_row, event_type: str, payload: dict) -> None:
    """Best-effort ledger event so re-dispatch attempts are counted + auditable."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await append_event(
                conn,
                actor_id=SYSTEM_ACTOR_ID,
                actor_type="system",
                event_type=event_type,
                payload={**payload,
                         "task_id": task_row["id"],
                         "story_id": task_row.get("story_id") or ""},
                context_goal_id=task_row.get("goal_id") or "",
                context_task_id=task_row["id"],
            )
    except Exception:
        _log.debug("dag_janitor: ledger emit failed for %s (%s)", task_row["id"], event_type)
