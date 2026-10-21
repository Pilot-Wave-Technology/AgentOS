"""Shared handler for card responses (approve, changes, replan, answer).

This encapsulates the logic that was originally inline in chat.py's WebSocket
handler so non-WS callers (Slack interactive actions, future CLI / API) can
dispatch card responses through the same agent-reinvocation path.

Entry point: `handle_card_response()` below. The function:
  1. Loads the task and derives source thread / correlation id
  2. Records the user's answer in the source thread (QUESTION_ANSWERED ledger
     event + INSERT into messages)
  3. Branches by (task.state, option_id):
       - state=blocked + option=replan → cancel siblings, reset parent, trigger
       - state=blocked + task.instance_id → inject answer, unblock
       - state=awaiting_approval + option=approve → create subtasks, dispatch
       - state=awaiting_approval + option=changes → feedback to planner
  4. Auto-saves Q/A or approval to goal memory when applicable.

Returns a `CardResponseResult` describing what happened so callers (e.g.
Slack) can surface a confirmation to the user.
"""
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.payloads import question_answered_payload, task_status_changed_payload
from app.ledger.schema import QUESTION_ANSWERED, TASK_STATUS_CHANGED
from app.openclaw import gateway_admin, make_task_session_key
from app.task_controller import (
    emit_task_event,
    get_last_event_id,
    get_task_correlation_id,
)

_log = logging.getLogger(__name__)


@dataclass
class CardResponseResult:
    ok: bool
    action: str                 # "answered" | "replanned" | "approved" | "changes_requested" | "noop"
    task_id: str
    source_thread_id: str = ""
    error: str = ""
    user_message: str = ""      # short, human-readable result for callers to surface


async def _last_thread_event_id(conn, thread_id: str) -> Optional[str]:
    row = await conn.fetchval(
        """SELECT event_id FROM ledger_events
           WHERE payload->>'thread_id' = $1
           ORDER BY sequence_number DESC LIMIT 1""",
        thread_id,
    )
    return str(row) if row else None


async def handle_card_response(
    task_id: str,
    option_id: str,
    value: str,
    user_uid: str,
) -> CardResponseResult:
    """End-to-end card response handler. See module docstring for details."""
    if not task_id or not value:
        return CardResponseResult(
            ok=False, action="noop", task_id=task_id,
            error="task_id and value required",
        )

    pool = get_pool()

    # Resolve actor_id for the responding user
    user_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", user_uid,
    )
    if not user_actor_id:
        return CardResponseResult(
            ok=False, action="noop", task_id=task_id,
            error=f"unknown user {user_uid}",
        )

    task_row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if not task_row:
        return CardResponseResult(
            ok=False, action="noop", task_id=task_id, error="task not found",
        )
    task = dict(task_row)

    # CROSS-GOAL IDOR. The task id arrives from outside -- a Slack action_id,
    # a modal's private_metadata, a websocket frame -- and proving the caller
    # is *a* user proves nothing about whether they may touch THIS task.
    # chat.py:1550-1559 scopes its own fetch to the goal the socket is
    # authorized for and says why; the Slack path had no equivalent, so a
    # mapped Slack user could answer, approve or replan any task in any goal
    # by id. The check lives HERE rather than at each call site because the
    # call sites are the thing that keeps growing: button, modal submit,
    # websocket, and whatever comes next.
    from app.auth import get_goal_role  # noqa: PLC0415
    if await get_goal_role(user_uid, task.get("goal_id") or "") is None:
        _log.warning(
            "card response REFUSED: uid=%s is not a member of goal=%s (task=%s)",
            user_uid, task.get("goal_id"), task_id,
        )
        return CardResponseResult(
            ok=False, action="noop", task_id=task_id,
            error="not authorized for this goal",
        )

    # Kill switch — refuse to act on a card (answer / approve / request-changes /
    # replan) while the task's goal or the system is frozen. Every branch below
    # either injects the human's text into the LIVE agent session or mutates task
    # rows and wakes the controller — all of which would bypass the freeze. Return
    # a clear noop so the caller (Slack action / browser WS) tells the user to
    # resume first. dispatch_block fails closed.
    from app.services import execution_control  # noqa: PLC0415
    _blk = await execution_control.dispatch_block(task.get("goal_id") or "")
    if _blk is not None:
        _log.info(
            "card response refused — execution paused (scope=%s) task=%s uid=%s",
            _blk.scope, task_id, user_uid,
        )
        return CardResponseResult(
            ok=False, action="noop", task_id=task_id, error="execution_paused",
            user_message=(f"⏸ Execution is paused ({_blk.scope}). "
                          "Resume in the control panel, then respond again."),
        )

    source_thread_id = task.get("source_thread_id") or ""
    thread_corr_id = None
    if source_thread_id:
        _raw_corr = await pool.fetchval(
            "SELECT correlation_id FROM threads WHERE id = $1", source_thread_id,
        )
        thread_corr_id = str(_raw_corr) if _raw_corr else None

    # 1. Record user's answer in the source thread
    msg_id = str(uuid.uuid4())
    display_value = f"[Re: {task_id}] {value}"
    if source_thread_id:
        async with pool.acquire() as conn:
            async with conn.transaction():
                last_event_id = await _last_thread_event_id(conn, source_thread_id)

                await append_event(
                    conn,
                    event_type=QUESTION_ANSWERED,
                    payload=question_answered_payload(
                        answer=value,
                        answerer={
                            "actor_id": str(user_actor_id),
                            "actor_type": "human",
                            "display_name": user_uid,
                        },
                        question_event_id=last_event_id or "",
                    ),
                    actor_id=user_actor_id,
                    correlation_id=thread_corr_id,
                    causation_ids=[last_event_id] if last_event_id else None,
                    context_goal_id=task.get("goal_id", ""),
                    context_conversation_id=source_thread_id,
                    context_task_id=task_id,
                )
                await conn.execute(
                    """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                       VALUES ($1, $2, $3, 'human', $4, 0)""",
                    msg_id, source_thread_id, user_uid, display_value,
                )

    # 2. Branch by state + option
    state = task.get("state", "")
    parent_tid = task.get("parent_task_id") or task_id
    task_corr = await get_task_correlation_id(pool, task_id)
    task_last = await get_last_event_id(pool, task_id)

    action = "noop"
    user_message = ""

    # ── Blocked + replan: cancel siblings, reset parent, re-dispatch planner ──
    if state == "blocked" and option_id == "replan":
        _log.info(
            "REPLAN via card: task=%s parent=%s value_len=%d",
            task_id, parent_tid, len(value),
        )
        await pool.execute(
            "UPDATE tasks SET blocked_reason = $1 WHERE id = $2",
            f"HUMAN FEEDBACK: {value}", parent_tid,
        )
        cancelled_rows = await pool.fetch("""
            UPDATE tasks SET state = 'cancelled'
             WHERE parent_task_id = $1
               AND state IN ('pending', 'ready', 'dispatching', 'blocked')
               AND status = 'active'
            RETURNING id
        """, parent_tid)
        # Release credentials for any cancelled subtask that had a mint
        # (typically only 'dispatching'/'blocked' tasks carry a handle).
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        for r in cancelled_rows:
            await release_credentials_safe(r["id"], "card_responses:replan_cancel")
        await pool.execute(
            "UPDATE tasks SET state = 'ready', result = '' WHERE id = $1",
            parent_tid,
        )
        await emit_task_event(
            pool,
            event_type=TASK_STATUS_CHANGED,
            task_id=parent_tid,
            goal_id=task["goal_id"],
            payload=task_status_changed_payload(
                previous_status="in_progress",
                new_status="ready",
                reason=f"Re-plan requested: {value[:200]}",
            ),
            actor_id=user_actor_id,
            correlation_id=task_corr,
            causation_ids=[task_last] if task_last else None,
            summary=f"Re-plan by {user_uid}",
        )
        from app.task_controller import task_controller
        await task_controller.trigger()
        action = "replanned"
        user_message = "Re-plan queued — the planner will revise."

    # ── Blocked + simple answer: inject answer, unblock ──
    elif state == "blocked" and task.get("instance_id"):
        agent_session_key = make_task_session_key(task['instance_id'], parent_tid)
        try:
            await gateway_admin.call("agent", {
                "agentId": task["instance_id"],
                "sessionKey": agent_session_key,
                "message": f"Human response to your question:\n\n{value}",
                "idempotencyKey": f"answer-{msg_id}",
            })
            _log.info(
                "Routed answer to agent %s for task %s",
                task["instance_id"], task_id,
            )
        except Exception as exc:
            _log.warning(
                "Failed to route answer to agent %s: %s",
                task["instance_id"], exc,
            )

        await pool.execute(
            "UPDATE tasks SET state = 'in_progress', blocked_reason = NULL WHERE id = $1",
            task_id,
        )
        await emit_task_event(
            pool,
            event_type=TASK_STATUS_CHANGED,
            task_id=task_id, goal_id=task["goal_id"],
            payload=task_status_changed_payload(
                previous_status="blocked", new_status="in_progress",
                reason=f"Human answered: {value[:200]}",
            ),
            actor_id=user_actor_id,
            correlation_id=task_corr,
            causation_ids=[task_last] if task_last else None,
            summary=f"Unblocked by {user_uid}",
        )
        action = "answered"
        user_message = "Answer sent to the agent."

    # ── Approve plan: create subtasks, move to in_progress ──
    elif state == "awaiting_approval" and option_id == "approve":
        from app.task_controller import create_subtasks_from_planner, task_controller
        _log.info("Plan approved for task %s — creating subtasks", task_id)

        # THE TRANSITION IS THE LOCK. This used to check the state, create
        # the subtasks, and THEN update -- so two clicks both passed the
        # check and both created a subtask tree. Not theoretical: the
        # interactive endpoint used to blow Slack's 3s budget and show the
        # user a timeout on a click that had in fact worked, which is an
        # invitation to click again. Claim the transition first; whoever
        # loses the race does nothing.
        # Same shape as diff_review.py's decided_at CAS, and for the same
        # reason.
        claimed = await pool.fetchval(
            "UPDATE tasks SET state = 'in_progress' "
            " WHERE id = $1 AND state = 'awaiting_approval' RETURNING id",
            task_id,
        )
        if not claimed:
            _log.info("Plan approval for task %s lost the race — already claimed",
                      task_id)
            return CardResponseResult(
                ok=True, action="noop", task_id=task_id,
                user_message="That plan has already been approved.",
            )

        result = task.get("result", "")
        if result:
            await create_subtasks_from_planner(task, result)
        await emit_task_event(
            pool,
            event_type=TASK_STATUS_CHANGED,
            task_id=task_id, goal_id=task["goal_id"],
            payload=task_status_changed_payload(
                previous_status="awaiting_approval", new_status="in_progress",
                reason="Plan approved by human",
            ),
            actor_id=user_actor_id,
            correlation_id=task_corr,
            causation_ids=[task_last] if task_last else None,
            summary="Plan approved — dispatching subtasks",
        )
        await task_controller.trigger()
        action = "approved"
        user_message = "Plan approved — dispatching subtasks."

    # ── Request plan changes: feedback to planner ──
    elif state == "awaiting_approval" and option_id in ("changes", "request_changes"):
        _log.info("Plan changes requested for task %s: %s", task_id, value[:100])

        _pw = await pool.fetchrow("""
            SELECT gateway_agent_id FROM agent_instances
             WHERE parent_task_id = $1 AND status = 'active'
               AND worker_type = 'persistent'
             LIMIT 1
        """, task_id)

        if _pw and task.get("instance_id"):
            pw_session_key = make_task_session_key(_pw['gateway_agent_id'], task_id)
            revision_msg = (
                f'REVISION REQUESTED by {user_uid}:\n"{value}"\n\n'
                "You are still the PLANNER. Revise your plan based on this feedback.\n"
                "You have full context of your original plan in the session history.\n"
                "Update the subtask list and call task-report with the revised plan.\n"
                "Do NOT start implementing — only revise the plan."
            )
            try:
                await gateway_admin.call("agent", {
                    "agentId": _pw["gateway_agent_id"],
                    "sessionKey": pw_session_key,
                    "message": revision_msg,
                    "idempotencyKey": f"revision-{task_id}-{msg_id}",
                })
            except Exception as exc:
                _log.warning("Failed to send revision to persistent worker: %s", exc)
            await pool.execute(
                "UPDATE tasks SET state = 'in_progress', blocked_reason = $1 WHERE id = $2",
                f"HUMAN FEEDBACK: {value}", task_id,
            )
        else:
            await pool.execute(
                "UPDATE tasks SET state = 'ready', result = NULL, blocked_reason = $1 WHERE id = $2",
                f"HUMAN FEEDBACK: {value}", task_id,
            )

        await emit_task_event(
            pool,
            event_type=TASK_STATUS_CHANGED,
            task_id=task_id, goal_id=task["goal_id"],
            payload=task_status_changed_payload(
                previous_status="awaiting_approval",
                new_status="in_progress" if _pw else "ready",
                reason=f"Plan changes requested: {value[:200]}",
            ),
            actor_id=user_actor_id,
            correlation_id=task_corr,
            causation_ids=[task_last] if task_last else None,
            summary=f"Replanning: {value[:100]}",
        )
        if not _pw:
            from app.task_controller import task_controller
            await task_controller.trigger()

        # Notify the GO so it can evaluate plan impact (update acceptance
        # criteria, coordinate with the assistant, etc.). This covers the
        # review phase — the human clicked Request Changes on a plan.
        try:
            from app.services.go_events import on_task_feedback
            await on_task_feedback(
                task["goal_id"], task_id, feedback=value,
                actor_uid=user_uid, phase="review",
            )
        except Exception as exc:
            _log.warning("on_task_feedback failed for %s: %s", task_id, exc)

        action = "changes_requested"
        user_message = "Change request sent to the planner."

    else:
        _log.info(
            "Card response noop: task=%s state=%s option=%s",
            task_id, state, option_id,
        )
        user_message = "Response recorded."

    # 3. Auto-save to goal memory (best-effort, all branches)
    try:
        if action in ("answered", "replanned"):
            from app.memory import auto_save_question_answered
            user_row = await pool.fetchrow(
                "SELECT display_name FROM users WHERE uid = $1", user_uid,
            )
            uname = user_row["display_name"] if user_row else user_uid
            question_text = task.get("blocked_reason", "")
            await auto_save_question_answered(
                pool, task_id, task["goal_id"],
                question_text, value, uname, user_uid,
            )
        elif action == "approved":
            from app.memory import auto_save_plan_approved
            user_row = await pool.fetchrow(
                "SELECT display_name FROM users WHERE uid = $1", user_uid,
            )
            uname = user_row["display_name"] if user_row else user_uid
            result = task.get("result", "")
            await auto_save_plan_approved(
                pool, task_id, task["goal_id"],
                uname, user_uid, len(result.split("```")) // 2,
            )
    except Exception as exc:
        _log.warning("Card response memory save failed for task %s: %s", task_id, exc)

    return CardResponseResult(
        ok=True, action=action, task_id=task_id,
        source_thread_id=source_thread_id,
        user_message=user_message or "Response recorded.",
    )
