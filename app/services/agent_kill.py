"""Kill switch: the HARD per-agent action — force-kill a running turn.

Kill is a ONE-WAY DOOR. A force-killed task lands in the terminal ``halted``
state; there is no restart and no resume. To redo the work an operator creates a
new task or (for claude-code-phase) starts a fresh plan via the SHCA. This is
deliberate: once we decide an agent is corrupted/looping and tear it down, its
in-flight context is gone, so pretending it can be resumed would be a lie.

Graceful per-instance *stop/resume* is a separate, fully-resumable feature — just
an ``agent_instance``-scope pause via ``execution_control`` (the dispatch gate
then skips the instance's next subtask); that lives in the admin router. This
module owns only the destructive path used when a turn won't finish on its own:

  * IA / conversational turn  → gateway ``chat.abort`` (registered run) + tear down
                                the task-scoped instance via ``destroy_instance``.
  * claude-code-phase turn    → gateway ``shca.claude.abort {taskId}`` (a custom
                                RPC we add to cli-spawn; see agent_os_openclaw). The
                                SHCA instance is long-lived and is NOT destroyed —
                                only the in-flight claude child is killed; the whole
                                story is then reset to planning (workspace + session
                                deleted) so it can be re-planned from scratch.

The task is moved to the terminal ``halted`` state (guarded so a late gateway
response can't clobber it) and credentials are released.

When the gateway could NOT confirm the child was actually stopped (older gateway
without the abort RPC, or the RPC errored), the result carries a ``warning`` — the
local state is halted but the agent may keep running until its own ~30-min
timeout. The router surfaces that to the operator so the "kill" is never silently
a no-op.

This module performs the gateway + DB work and returns a result dict. The admin
router owns auth and emits the ``execution.*`` ledger events, so the audit trail
stays in one place.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError, make_goal_session_key, make_task_session_key

_log = logging.getLogger(__name__)

# Task states from which a force-kill is meaningful (something is/was running).
# The halt UPDATE guards filter on this so a late gateway response can't clobber
# a task that already moved to a terminal state.
_KILLABLE_STATES = ["dispatching", "in_progress", "blocked", "ready"]


async def force_kill_task(task_id: str, reason: str, by_uid: str) -> dict:
    """Force-kill the agent running ``task_id`` and park the task in ``halted``.

    Returns a result dict the router uses for its ledger event:
      {ok, task_id, goal_id, executor, aborted, instance_destroyed, error?}

    Best-effort on the gateway side: if the abort RPC is unavailable (older
    gateway) or the agent is already gone, we still halt the task locally — the
    state guard makes any late gateway response a no-op, and the operator's
    intent is recorded.
    """
    pool = get_pool()
    task = await pool.fetchrow(
        "SELECT * FROM tasks WHERE id = $1 AND status = 'active'", task_id
    )
    if task is None:
        _log.warning("force_kill_task: task %s not found / not active", task_id)
        return {"ok": False, "error": "task not found", "task_id": task_id}

    task = dict(task)
    goal_id = task.get("goal_id", "")
    executor = (task.get("executor") or "ia").strip()
    instance_id = task.get("instance_id") or ""
    aborted = False
    instance_destroyed = False
    # Set when the gateway could not confirm the in-flight child was stopped, so
    # the router can warn the operator that the agent may run on until its own
    # ~30-min timeout despite the task being halted locally.
    warning = ""

    _log.info(
        "force_kill_task: task=%s goal=%s executor=%s instance=%s by=%s reason=%s",
        task_id, goal_id, executor, instance_id or "-", by_uid or "-", reason or "<none>",
    )

    if executor == "claude-code-phase":
        # All claude-code-phase tasks under a story share ONE workspace and ONE
        # Claude session — killing any single phase task makes the whole story's
        # context unrecoverable. We therefore treat kill as story-scoped:
        #   1. Abort the in-flight subprocess      (only if dispatched)
        #   2. Cancel every other phase task in the story (pending/ready/etc.)
        #   3. Delete the shared workspace on the VM  (only if dispatched)
        #   4. Delete the shared Claude session on disk  (only if dispatched)
        #   5. Reset the story to planning so it can be re-provisioned from scratch  (only if dispatched)
        # M14: steps 1/3/4/5 only run when the task was actually dispatched
        # (in_progress or dispatching). For ready/blocked tasks no subprocess
        # exists and the workspace/session are still valid — tearing them down
        # would destroy context for other phases that haven't run yet.
        # The SHCA agent stays alive — the user talks to it to start a new plan.
        story_id = task.get("story_id") or ""
        _was_dispatched = task.get("state") in ("in_progress", "dispatching")

        # Step 1: abort in-flight subprocess (only if the task was dispatched).
        if _was_dispatched:
            try:
                res = await gateway_admin.call("shca.claude.abort", {"taskId": task_id})
                aborted = bool(res.get("aborted")) if isinstance(res, dict) else True
                _log.info("force_kill_task: shca.claude.abort sent for task=%s (aborted=%s)", task_id, aborted)
                if not aborted:
                    warning = ("Gateway reported no live agent to kill — the turn may have "
                               "already finished, or this agent could still be running.")
            except GatewayError as exc:
                warning = ("Could not reach the gateway to kill the agent — it may keep "
                           "running until its own ~30-min timeout. Task halted locally anyway.")
                _log.warning(
                    "force_kill_task: shca.claude.abort failed for task=%s (%s) — "
                    "child may run until its own 30-min timeout",
                    task_id, exc,
                )
        else:
            _log.info(
                "force_kill_task: task=%s state=%s — skipping abort (never dispatched)",
                task_id, task.get("state"),
            )

        if story_id and goal_id:
            # Step 2: cancel ALL sibling phase tasks so none re-dispatch.
            # Use a state guard so already-done tasks are not disturbed.
            # Note: PostgreSQL's RETURNING cannot contain aggregate functions
            # like COUNT(*) — it returns one row per updated row, not a total.
            # Use a separate SELECT count after the UPDATE instead.
            await pool.execute(
                """UPDATE tasks
                      SET state = 'cancelled', blocked_reason = $1, updated_at = NOW()
                    WHERE story_id = $2
                      AND executor = 'claude-code-phase'
                      AND state NOT IN ('done', 'cancelled', 'halted')""",
                "Story killed by operator — all phases reset; restart from scratch",
                story_id,
            )
            cancelled_count = await pool.fetchval(
                """SELECT COUNT(*) FROM tasks
                    WHERE story_id = $1
                      AND executor = 'claude-code-phase'
                      AND state = 'cancelled'""",
                story_id,
            )
            _log.info(
                "force_kill_task: cancelled %s sibling phase task(s) for story=%s",
                cancelled_count or 0, story_id,
            )

            # Steps 3 + 4: delete workspace and Claude session. Skipped for
            # never-dispatched tasks (ready/blocked) — no subprocess ran so the
            # workspace is untouched; tearing it down would destroy valid context
            # for other phases in this story (M14).
            if _was_dispatched:
                try:
                    from app.database import get_pool as _get_pool  # noqa: PLC0415
                    _pool = _get_pool()
                    story_row = await _pool.fetchrow(
                        """SELECT active_thread_id, current_claude_session_id,
                                  shca_instance_id
                             FROM stories WHERE id = $1""",
                        story_id,
                    )
                    if story_row:
                        thread_id = story_row["active_thread_id"] or ""
                        claude_session_id = story_row["current_claude_session_id"] or ""

                        # Delete workspace (repo checkout + credential files).
                        if thread_id:
                            template_agent_id = await _pool.fetchval(
                                "SELECT agent_id FROM threads WHERE id = $1", thread_id,
                            )
                            if template_agent_id:
                                from app.services.operator_provisioning import delete_story_workspace  # noqa: PLC0415
                                ws_result = await delete_story_workspace(
                                    goal_id=goal_id, story_id=story_id,
                                    agent_id=template_agent_id, thread_id=thread_id,
                                )
                                instance_destroyed = ws_result.get("ok", False)
                                _log.info(
                                    "force_kill_task: workspace deleted for story=%s ok=%s",
                                    story_id, instance_destroyed,
                                )

                        # Delete the shared Claude session transcript on disk so a
                        # future re-provision gets a clean slate, not a corrupted history.
                        if claude_session_id and story_row["shca_instance_id"]:
                            instance_id_for_session = story_row["shca_instance_id"]
                            session_key = make_goal_session_key(instance_id_for_session, goal_id, thread_id)
                            try:
                                await gateway_admin.delete_session(session_key, delete_transcript=True)
                                _log.info(
                                    "force_kill_task: claude session deleted for story=%s session=%s",
                                    story_id, claude_session_id[:12],
                                )
                            except Exception as exc:
                                _log.warning(
                                    "force_kill_task: session delete failed for story=%s: %s",
                                    story_id, exc,
                                )
                except Exception as exc:
                    _log.warning(
                        "force_kill_task: workspace/session cleanup failed for story=%s: %s",
                        story_id, exc,
                    )

                # Step 5: reset story to planning so SHCA can start a new plan.
                # current_claude_session_id cleared — future dispatch gets a fresh session.
                await pool.execute(
                    """UPDATE stories
                          SET current_phase             = 'planning',
                              current_plan_path         = NULL,
                              execution_paused          = FALSE,
                              current_claude_session_id = NULL,
                              workspace_path            = NULL,
                              workspace_state           = 'deleted',
                              updated_at                = NOW()
                        WHERE id = $1""",
                    story_id,
                )
                _log.info(
                    "force_kill_task: story=%s reset to planning — user must start a new plan",
                    story_id,
                )
            else:
                _log.info(
                    "force_kill_task: task=%s was never dispatched — "
                    "skipping workspace/session teardown and story reset for story=%s",
                    task_id, story_id,
                )

        # Mark the triggering task halted (the cancelled siblings are already done above).
        halt_reason = f"Force-killed by {by_uid or 'operator'}: {reason}" if reason else \
            f"Force-killed by {by_uid or 'operator'}; entire story reset — start a new plan"
        await pool.execute(
            """UPDATE tasks SET state = 'halted', blocked_reason = $1, updated_at = NOW()
                WHERE id = $2 AND state = ANY($3::text[])""",
            halt_reason, task_id, _KILLABLE_STATES,
        )
        # Release credentials for this task and all cancelled siblings.
        try:
            from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
            await release_credentials_safe(task_id, "agent_kill:force_kill_phase")
            if story_id:
                # Only release credentials for tasks that were still running —
                # done/approved tasks already released theirs at completion.
                sibling_tasks = await pool.fetch(
                    """SELECT id FROM tasks WHERE story_id = $1
                        AND executor = 'claude-code-phase' AND id != $2
                        AND state NOT IN ('done', 'approved', 'cancelled')""",
                    story_id, task_id,
                )
                for sibling in sibling_tasks:
                    await release_credentials_safe(sibling["id"], "agent_kill:force_kill_phase_sibling")
        except Exception as exc:
            _log.debug("force_kill_task: credential release failed: %s", exc)

        return {
            "ok": True,
            "task_id": task_id,
            "goal_id": goal_id,
            "executor": executor,
            "aborted": aborted,
            "instance_destroyed": instance_destroyed,
            "story_reset": bool(story_id),
            "warning": warning,
        }

    else:
        # IA / conversational: abort the registered run, then tear down the instance.
        # Persistent workers: all subtasks of a parent share ONE gateway agent instance
        # keyed by parent_task_id. If this task is a subtask (has parent_task_id set),
        # destroying the instance would orphan every other sibling subtask on the same
        # worker. Abort the current turn only; leave the instance alive so siblings
        # keep their workspace. The instance is cleaned up by cleanup_parent_task()
        # when the parent reaches a terminal state.
        if instance_id:
            parent_task_id = task.get("parent_task_id") or ""
            session_task = parent_task_id or task_id
            session_key = make_task_session_key(instance_id, session_task)
            try:
                await gateway_admin.chat_abort(session_key)
                aborted = True
                _log.info("force_kill_task: chat.abort sent for session=%s", session_key)
            except GatewayError as exc:
                warning = ("Could not reach the gateway to abort the agent turn — it may "
                           "keep running briefly. Task halted locally anyway.")
                _log.warning("force_kill_task: chat.abort failed for session=%s (%s)", session_key, exc)

            if parent_task_id:
                # Subtask on a persistent worker — do NOT destroy the shared instance.
                _log.info(
                    "force_kill_task: task=%s is a subtask of parent=%s (persistent worker) — "
                    "skipping destroy_instance to avoid orphaning sibling subtasks",
                    task_id, parent_task_id,
                )
            else:
                # Top-level IA task owns its own instance — safe to destroy.
                try:
                    from app.task_controller import destroy_instance  # noqa: PLC0415
                    await destroy_instance(task)
                    instance_destroyed = True
                except Exception as exc:
                    _log.warning("force_kill_task: destroy_instance failed for task=%s: %s", task_id, exc)
        else:
            _log.info("force_kill_task: task=%s has no instance_id — nothing to abort/destroy", task_id)

    # Release any minted credentials so an IAM key / workspace file doesn't linger.
    try:
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        await release_credentials_safe(task_id, "agent_kill:force_kill")
    except Exception as exc:
        _log.debug("force_kill_task: credential release for %s failed: %s", task_id, exc)

    # Move to 'halted'. The state guard means a late gateway/sentinel response
    # (e.g. a phase turn that finishes after we abort) becomes a no-op instead of
    # clobbering the operator's halt.
    halt_reason = f"Force-killed by {by_uid or 'operator'}: {reason}" if reason else \
        f"Force-killed by {by_uid or 'operator'}; review before restart"
    result = await pool.execute(
        """UPDATE tasks
              SET state = 'halted', blocked_reason = $1, updated_at = NOW()
            WHERE id = $2
              AND state = ANY($3::text[])""",
        halt_reason, task_id, _KILLABLE_STATES,
    )
    if result == "UPDATE 0":
        _log.info("force_kill_task: task %s was not in a killable state — already terminal?", task_id)

    return {
        "ok": True,
        "task_id": task_id,
        "goal_id": goal_id,
        "executor": executor,
        "aborted": aborted,
        "instance_destroyed": instance_destroyed,
        "warning": warning,
    }


async def resolve_task_for_instance(instance_id: str) -> Optional[dict]:
    """Find the task currently running on a gateway instance (for the admin UI's
    per-instance force-kill button). Returns the task dict or None."""
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT * FROM tasks
            WHERE instance_id = $1 AND status = 'active'
              AND state IN ('dispatching', 'in_progress', 'blocked')
            ORDER BY created_at DESC LIMIT 1""",
        instance_id,
    )
    return dict(row) if row else None
