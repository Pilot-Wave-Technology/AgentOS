"""Gate task selection and dispatch for the integration flow.

Mirrors phase_dispatcher.py's single-flight pattern but for executor='system-gate'
tasks. Both selectors use the same broadened busy_stories definition (active phase
tasks AND active gates) to prevent concurrent work on the same story.
"""
from __future__ import annotations

import logging

from app.database import get_pool

_log = logging.getLogger(__name__)


async def find_next_dispatchable_gate() -> dict | None:
    """Return one ready system-gate task whose story has no active phase or gate work.

    Mirrors find_next_dispatchable_task() — same kill-switch checks, same single-flight
    guard, same goal-freeze exclusion. The busy_stories CTE here is the gate-side mirror
    of the broadened CTE in phase_dispatcher (they must be kept in sync).
    """
    pool = get_pool()

    from app.services import execution_control  # noqa: PLC0415
    if await execution_control.is_system_paused():
        _log.info("merge_orchestrator.dispatch: gate selection suppressed — SYSTEM paused")
        return None

    row = await pool.fetchrow(
        """
        WITH busy_stories AS (
            -- Same definition as phase_dispatcher.find_next_dispatchable_task.
            -- A story is busy if it has an active phase task OR an active gate.
            SELECT DISTINCT story_id FROM tasks
             WHERE story_id IS NOT NULL
               AND (
                   (executor = 'claude-code-phase'
                    AND state IN ('in_progress', 'dispatching', 'blocked'))
                   OR
                   (executor = 'system-gate'
                    AND state IN ('in_progress', 'dispatching', 'awaiting_approval'))
               )
        )
        SELECT t.*
          FROM tasks t
          JOIN stories s ON s.id = t.story_id
         WHERE t.executor = 'system-gate'
           AND t.state = 'ready'
           AND t.story_id IS NOT NULL
           -- Deleted-goal guard (mirror of phase_dispatcher): goal delete is soft
           -- and doesn't cascade to tasks, so skip gates for a dead goal.
           AND EXISTS (
               SELECT 1 FROM goals g
                WHERE g.id = t.goal_id AND g.status = 'active'
           )
           AND t.story_id NOT IN (SELECT story_id FROM busy_stories)
           AND s.execution_paused = FALSE
           -- Local Execution Mode (M0): never dispatch a checked-out / local story.
           AND s.checked_out_to_uid IS NULL
           AND s.execution_mode <> 'local'
           AND NOT EXISTS (
               SELECT 1 FROM execution_pauses ep
                WHERE ep.scope = 'goal' AND ep.scope_id = t.goal_id
           )
         ORDER BY t.created_at ASC
         LIMIT 1
        """,
    )
    return dict(row) if row else None


async def dispatch_system_gate(gate: dict) -> None:
    """Transition a ready gate task to in_progress and call the appropriate handler.

    The transition is atomic (WHERE state='ready') to prevent double-dispatch if
    two ticks fire close together. A task already taken by another tick is logged
    and skipped — not an error.
    """
    pool = get_pool()
    task_id = gate["id"]
    gate_kind = gate.get("gate_kind")
    story_id = gate.get("story_id")

    _log.info(
        "merge_orchestrator.dispatch: starting gate %s kind=%s story=%s",
        task_id, gate_kind, story_id,
    )

    updated = await pool.fetchval(
        """UPDATE tasks SET state = 'in_progress', updated_at = NOW()
           WHERE id = $1 AND state = 'ready'
           RETURNING id""",
        task_id,
    )
    if not updated:
        _log.warning(
            "merge_orchestrator.dispatch: gate %s already taken (state != ready), skipping",
            task_id,
        )
        return

    try:
        if gate_kind == "pre-integration":
            from app.services.merge_orchestrator.handlers.pre_integration import (  # noqa: PLC0415
                handle_pre_integration_gate,
            )
            await handle_pre_integration_gate(gate, pool)

        elif gate_kind == "integrate":
            from app.services.merge_orchestrator.handlers.integrate import (  # noqa: PLC0415
                handle_integrate_gate,
            )
            await handle_integrate_gate(gate, pool)

        else:
            _log.error(
                "merge_orchestrator.dispatch: unknown gate_kind=%r for task %s — marking failed",
                gate_kind, task_id,
            )
            await pool.execute(
                "UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1",
                task_id,
            )

    except Exception:
        _log.exception(
            "merge_orchestrator.dispatch: gate %s (kind=%s) raised unhandled exception — marking failed",
            task_id, gate_kind,
        )
        await _fail_gate_safely(pool, task_id)
        await on_gate_failed(gate, f"Unhandled exception in gate handler (kind={gate_kind!r}). Check Cloud Run logs for the full traceback.")


async def _fail_gate_safely(pool, task_id: str) -> None:
    """Mark the gate failed without raising, so dispatch caller always returns cleanly."""
    try:
        await pool.execute(
            "UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1",
            task_id,
        )
    except Exception:
        _log.exception("merge_orchestrator.dispatch: could not mark gate %s failed", task_id)


async def on_gate_failed(gate: dict, reason: str) -> None:
    """Called by gate handlers when they mark a gate failed.

    Two responsibilities:
      1. Cascade failure — mark all downstream pending/ready tasks failed so the
         DAG halts. Without this, tasks that depend on the gate stay pending
         forever and the DAG never terminates cleanly.
      2. Notify SHCA — relay a message to the story's SHCA thread so the human
         knows the gate failed and why. Gates are invisible to the agent otherwise.
    """
    pool = get_pool()
    task_id = gate["id"]
    story_id = gate.get("story_id")
    goal_id = gate.get("goal_id")
    gate_kind = gate.get("gate_kind", "")

    _log.warning(
        "merge_orchestrator.dispatch: on_gate_failed — gate=%s kind=%s story=%s reason=%r",
        task_id, gate_kind, story_id, reason,
    )

    # 1. Cascade: mark all tasks in this story that are still pending/ready as
    #    failed. This stops the integration-tester and any subsequent phases from
    #    running against an unprepared workspace.
    try:
        result = await pool.execute(
            """UPDATE tasks SET state = 'failed', updated_at = NOW()
               WHERE story_id = $1
                 AND state IN ('pending', 'ready')
                 AND id != $2""",
            story_id, task_id,
        )
        # asyncpg returns "UPDATE N" — parse the count for the log.
        cascaded = int(result.split()[-1]) if result else 0
        _log.warning(
            "merge_orchestrator.dispatch: cascade complete — story=%s gate=%s cascaded_tasks=%d",
            story_id, task_id, cascaded,
        )
    except Exception:
        _log.exception("merge_orchestrator.dispatch: cascade failure update failed for gate %s", task_id)

    # 2. Notify SHCA so the human sees the failure and can act.
    try:
        await _notify_shca_gate_failed(story_id, goal_id, task_id, gate_kind, reason)
    except Exception:
        _log.exception("merge_orchestrator.dispatch: SHCA notification failed for gate %s", task_id)


async def _notify_shca_gate_failed(
    story_id: str, goal_id: str, task_id: str, gate_kind: str, reason: str
) -> None:
    """Write a failure card to the story's active thread via write_task_progress_card.

    write_task_progress_card resolves the thread from stories.active_thread_id,
    persists the card to messages, broadcasts via WebSocket, and forwards to the
    SHCA's gateway session so the assistant's LLM sees the failure in real-time.
    """
    message = (
        f"[integration-gate] Gate **{gate_kind}** failed for this story.\n\n"
        f"**Reason:** {reason}\n\n"
        f"The DAG has been paused — all pending tasks have been stopped. "
        f"Review the gate failure and decide how to proceed:\n"
        f"- Fix the underlying issue and start a new story plan, or\n"
        f"- Check the Cloud Run logs for `gate={task_id}` for the full error detail."
    )
    from app.services.sentinel_parser import write_task_progress_card  # noqa: PLC0415
    await write_task_progress_card(
        goal_id=goal_id,
        story_id=story_id,
        task_id=task_id,
        phase=gate_kind,
        kind="gate_failed",
        text=message,
        sentinel_kind="GATE_FAILED",
    )
    _log.info(
        "merge_orchestrator.dispatch: gate failure card written for story=%s gate=%s",
        story_id, task_id,
    )
