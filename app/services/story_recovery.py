"""Claude-code story recovery actions (assistant-workflow refactor Phase 3).

Human-triggered recovery for a code story's DAG — replan / retry / abandon / reject_plan.
These mirror the diff-review reject pattern (mark the DAG terminal as needed → move the
story phase → post a task-controller message the assistant picks up from its next
snapshot). The IA-hierarchy recovery (tasks.py / card_responses.py replan) is NOT used —
these operate purely on the claude-code-dag model.

INVARIANT (Phase 2b W2): every path that resumes execution either creates a fresh DAG
(replan → planning → new ingest at approval) or resets the SAME DAG to a running state
AND leaves no stale failed/blocked child, so `next_action` never surfaces a superseded
state (a failed DAG becomes cancelled on replan/abandon; a retried DAG goes back to
in_progress with its children reset).
"""
from __future__ import annotations

import logging
import uuid

from app.database import get_pool
from app.ledger.journal import append_event
from app.openclaw import gateway_admin, GatewayError
from app.services.task_states import TERMINAL_PHASE_STATES, TERMINAL_TASK_STATES

_log = logging.getLogger(__name__)
from app.ledger.actors import SYSTEM_ACTOR_ID  # one canonical, registered actor


async def _refuse_if_frozen(goal_id: str, story_id: str, action: str, by_uid: str) -> dict | None:
    """Shared kill-switch guard for every recovery action below. All five mutate
    task/story rows and wake the controller, which would bypass a system/goal
    freeze the same way autonomous dispatch would. Used by both the web
    /stories/{sid}/recover endpoint and the Slack recover_* buttons (they call
    these functions directly), so one check here covers both surfaces."""
    from app.services import execution_control  # noqa: PLC0415
    frozen = await execution_control.frozen_response(goal_id)
    if frozen is not None:
        _log.info(
            "%s refused — execution paused, goal=%s story=%s uid=%s",
            action, goal_id, story_id, by_uid,
        )
    return frozen


async def workspace_has_changes(instance_id: str, story_id: str) -> tuple[bool, str]:
    """Return (has_uncommitted_changes, stat_summary) for the story's repo working tree.

    Used to warn before Abandon / Mark-done discard completed-phase work (phases don't
    commit — the DAG only commits+pushes at the diff-review gate after ALL subtasks are
    done, so abandoning a partial DAG loses everything the done phases produced). Fail-OPEN
    (returns False) on any gateway error: a diff-check hiccup must not hard-block the human
    from abandoning / closing a story — the warning is best-effort."""
    if not instance_id:
        return (False, "")
    try:
        r = await gateway_admin.call(
            "shca.workspace.gitDiff",
            {"agentId": instance_id, "storyId": story_id, "scope": "all"},
            timeout=30,
        )
    except GatewayError as exc:
        _log.warning("workspace_has_changes: gitDiff failed (story=%s): %s", story_id, exc)
        return (False, "")
    except Exception:
        _log.exception("workspace_has_changes: unexpected (story=%s)", story_id)
        return (False, "")
    diff = (r.get("diff") or "").strip()
    stat = (r.get("stat_summary") or "").strip()
    return (bool(diff), stat)


async def _emit(goal_id: str, event_type: str, payload: dict) -> None:
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await append_event(
                conn, event_type=event_type, context_goal_id=goal_id,
                actor_id=SYSTEM_ACTOR_ID, actor_type="system", payload=payload,
                correlation_id=str(uuid.uuid4()),
            )
    except Exception as exc:
        _log.warning("story_recovery: ledger emit %s failed: %s", event_type, exc)


async def _post(goal_id: str, story_id: str, task_id: str, phase: str,
                text: str, sentinel_kind: str) -> None:
    """Post a task-controller message the assistant sees (same plumbing diff_review uses)."""
    try:
        from app.services.sentinel_parser import write_task_progress_card
        await write_task_progress_card(
            goal_id=goal_id, story_id=story_id, task_id=task_id,
            phase=phase, kind="output", text=text, sentinel_kind=sentinel_kind,
        )
    except Exception:
        _log.exception("story_recovery: failed to post message (story=%s)", story_id)


async def post_recovery_card(goal_id: str, story_id: str, dag_task_id: str,
                             reason: str = "", mode: str = "failed") -> None:
    """Post a `dag_recovery` card (Replan / Retry / Abandon) to the story thread.

    mode="failed" (default): a DAG hard-failed — posted from the single deterministic
      fail point (sentinel_parser._fail_dag_parent).
    mode="blocked": a phase task is BLOCKED awaiting human input — posted from the
      BLOCKED branch alongside the "answer to resume" card, so the human has an escape
      hatch (retry / replan / abandon) if answering isn't the right move.

    Same card kind + options either way, so the chat UI (appendDagRecoveryCard) and Slack
    (_render_dag_recovery) render the buttons unchanged; only the title/body differ. The
    assistant also sees it (next_action routes failed/needs_answer to recovery)."""
    pool = get_pool()
    story = await pool.fetchrow("SELECT active_thread_id FROM stories WHERE id = $1", story_id)
    if not story or not story["active_thread_id"]:
        return
    thread_id = story["active_thread_id"]
    if not reason:
        failed = await pool.fetchrow(
            "SELECT blocked_reason, phase FROM tasks WHERE parent_task_id = $1 "
            "AND executor = 'claude-code-phase' AND state = 'failed' "
            "ORDER BY completed_at DESC NULLS LAST LIMIT 1",
            dag_task_id,
        )
        reason = (failed and failed["blocked_reason"]) or ""
    reason = (reason or ("The phase is blocked." if mode == "blocked" else "The DAG failed.")).strip()
    # `review_keep` salvages the completed phases' uncommitted work (capture diff → review
    # gate → Approve & Push) instead of losing it to Abandon. Abandon stays last (destructive).
    options = ["replan", "retry", "review_keep", "abandon"]
    import json as _json
    card = {"kind": "dag_recovery", "dag_task_id": dag_task_id, "story_id": story_id,
            "options": options, "reason": reason[:500], "mode": mode}
    if mode == "blocked":
        body = (
            f"# Phase blocked\n\n{reason[:1000]}\n\n"
            f"**Answer in chat to resume** this task — or, if answering isn't the right "
            f"move: **Retry** (re-run the phase without an answer — e.g. the blocker "
            f"cleared), **Replan** (new cycle, code kept), **Review & keep** (push what the "
            f"completed phases already produced), or **Abandon** (discard uncommitted work)."
        )
    else:
        body = (
            f"# Phase failed\n\n{reason[:1000]}\n\n"
            f"Choose how to recover: **Replan** (fix forward — new cycle, code kept), "
            f"**Retry** (re-run the failed phase — for flaky/infra failures), **Review & "
            f"keep** (push what the completed phases already produced), or **Abandon** "
            f"(discard uncommitted work)."
        )
    msg_id = str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, card_data, created_at) "
        "VALUES ($1, $2, 'agent', 'task-controller', $3, $4::JSONB, NOW())",
        msg_id, thread_id, body, _json.dumps(card),
    )
    # Live UI delivery via the durable fan-out (parity with diff_review): every
    # web instance re-broadcasts to its own subscribers, so the card reaches a
    # viewer whose WS is on a different instance — and it works even though this
    # runs in the task-controller path (no local browser session). The fan-out
    # broadcaster (client handler: appendDagRecoveryCard) rebuilds the
    # dag_recovery frame from card_data + the message body.
    try:
        from app.chat_module.repository import notify_new_message
        await notify_new_message(thread_id, msg_id)
    except Exception:
        _log.exception("post_recovery_card: fan-out notify failed (story=%s)", story_id)
    # The Slack relay used to be a second call here. notify_new_message
    # above now mirrors every card-bearing message, so relaying again
    # would post the card twice.



async def mark_recovery_cards_decided(dag_task_id: str, action: str) -> None:
    """Stamp `decided` on this DAG's recovery card(s) so the buttons don't re-appear on
    reload after the human picked an action (parity with diff-review's _mark_card_decided)."""
    try:
        pool = get_pool()
        await pool.execute(
            "UPDATE messages SET card_data = card_data || jsonb_build_object('decided', $2::text) "
            "WHERE card_data->>'kind' = 'dag_recovery' AND card_data->>'dag_task_id' = $1",
            dag_task_id, action,
        )
    except Exception:
        _log.warning("mark_recovery_cards_decided failed for dag %s", dag_task_id)


async def _current_dag(pool, story_id: str):
    return await pool.fetchrow(
        "SELECT id, goal_id, state FROM tasks WHERE story_id = $1 "
        "AND executor = 'claude-code-dag' ORDER BY created_at DESC, id DESC LIMIT 1",
        story_id,
    )


async def _close_dag_and_children(pool, dag_id: str, dag_state: str = "cancelled") -> None:
    """Terminate a DAG parent + any non-terminal children (used by replan/abandon)."""
    await pool.execute(
        "UPDATE tasks SET state = $2, updated_at = NOW() WHERE id = $1 AND state <> ALL($3)",
        dag_id, dag_state, list(TERMINAL_TASK_STATES),
    )
    await pool.execute(
        "UPDATE tasks SET state = 'cancelled', updated_at = NOW() "
        "WHERE parent_task_id = $1 AND executor = 'claude-code-phase' AND state <> ALL($2)",
        dag_id, list(TERMINAL_PHASE_STATES),
    )


async def replan_dag(story_id: str, feedback: str = "", by_uid: str = "") -> dict:
    """Send the current DAG's story back to `planning` for a NEW cycle (fix forward —
    uncommitted code is KEPT). Closes the DAG (+ any live children). The assistant's
    planning skill (MODE C) picks it up from the next snapshot and, once the new plan is
    approved, dag_ingest creates a fresh DAG."""
    pool = get_pool()
    dag = await _current_dag(pool, story_id)
    if not dag:
        return {"ok": False, "error": "no DAG for story"}
    goal_id = dag["goal_id"]
    if (_frozen := await _refuse_if_frozen(goal_id, story_id, "replan_dag", by_uid)) is not None:
        return _frozen
    await _close_dag_and_children(pool, dag["id"], dag_state="cancelled")
    await pool.execute(
        # New cycle: clear the plan-ready pointer so "Approve plan" stays disabled until
        # the new cycle's plan is written (keep current_plan_id for MODE-C detection).
        "UPDATE stories SET current_phase = 'planning', current_plan_path = NULL, "
        "updated_at = NOW() WHERE id = $1",
        story_id,
    )
    body = (
        f"♻ Replan requested. DAG `{dag['id']}` is closed; the story is back at "
        f"`planning` for a new cycle (existing code is kept — fix forward)."
    )
    if feedback:
        body += f"\n\nHuman feedback:\n> {feedback}"
    await _post(goal_id, story_id, f"{dag['id']}-replan", "planning", body, "REPLAN")
    await _emit(goal_id, "plan.replan_requested",
                {"story_id": story_id, "dag_task_id": dag["id"], "feedback": feedback, "by": by_uid})
    return {"ok": True, "story_id": story_id, "dag_task_id": dag["id"]}


async def retry_dag(story_id: str, by_uid: str = "") -> dict:
    """Retry the current DAG in place — re-run the failed OR blocked phase(s):
      - FAILED DAG (flaky / infra failure): re-run the failed portion.
      - BLOCKED phase (parent still `running`): the human would rather re-run than answer
        (e.g. an environmental/resource blocker cleared). NOTE: re-running a phase blocked
        on a QUESTION with no new input will usually just re-block on the same question —
        Answer (resume) or Replan is the right move there; Retry is for cleared blockers.
    Resets the failed/blocked/cascaded-cancelled children to `pending` (deps re-resolve to
    ready) and re-opens the parent; completed (`done`) phases are KEPT. Clears any pending
    blocker question so the Q&A relay no longer targets the task. Story stays in its
    execution phase; next_action returns to `ok`."""
    pool = get_pool()
    dag = await _current_dag(pool, story_id)
    if not dag:
        return {"ok": False, "error": "no DAG for story"}
    goal_id = dag["goal_id"]
    if (_frozen := await _refuse_if_frozen(goal_id, story_id, "retry_dag", by_uid)) is not None:
        return _frozen
    # Retryable when there is at least one child to re-run: a failed child (hard-failed
    # DAG) or a blocked child (running DAG). Gate on the children, not dag.state, so this
    # works for BOTH the failed-DAG and blocked-phase cases.
    resettable = await pool.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE parent_task_id = $1 "
        "AND executor = 'claude-code-phase' AND state IN ('failed', 'blocked', 'cancelled')",
        dag["id"],
    )
    if not resettable:
        return {"ok": False, "error": f"nothing to retry (DAG state={dag['state']}, no failed/blocked phase)"}
    # Reset failed/blocked/cancelled children to pending (deps re-resolve to ready); keep
    # 'done'. Clear the blocker question/reason so the Q&A relay stops targeting a task the
    # human chose to re-run instead of answer.
    await pool.execute(
        "UPDATE tasks SET state = 'pending', blocked_reason = NULL, "
        "last_blocker_question = NULL, completed_at = NULL, updated_at = NOW() "
        "WHERE parent_task_id = $1 AND executor = 'claude-code-phase' "
        "AND state IN ('failed', 'blocked', 'cancelled')",
        dag["id"],
    )
    await pool.execute(
        "UPDATE tasks SET state = 'in_progress', updated_at = NOW() WHERE id = $1",
        dag["id"],
    )
    # Root tasks (no unmet deps) → ready; wake the controller.
    from app.services.sentinel_parser import _resolve_dependents  # noqa: PLC0415
    await _resolve_dependents(story_id)
    await _post(goal_id, story_id, f"{dag['id']}-retry", "executing",
                f"↻ Retrying DAG `{dag['id']}` — re-dispatching the failed/blocked phase(s).", "RETRY")
    await _emit(goal_id, "dag.retried", {"story_id": story_id, "dag_task_id": dag["id"], "by": by_uid})
    return {"ok": True, "story_id": story_id, "dag_task_id": dag["id"]}


async def abandon_dag(story_id: str, by_uid: str = "", force: bool = False) -> dict:
    """Abandon the current DAG: cancel it (+ live children) and move the story to
    `awaiting_next` — the human can start a fresh plan or mark the story done.

    Phases don't commit; the DAG only commits+pushes at the diff-review gate after ALL
    subtasks are done. So abandoning a PARTIAL DAG discards everything the completed phases
    produced (it's uncommitted in repo/ and the workspace is later reaped). When there ARE
    such changes and `force` is not set, return `needs_confirm` so the caller can warn the
    human and offer Review & keep — this closes the silent-data-loss footgun."""
    pool = get_pool()
    dag = await _current_dag(pool, story_id)
    if not dag:
        return {"ok": False, "error": "no DAG for story"}
    goal_id = dag["goal_id"]
    if (_frozen := await _refuse_if_frozen(goal_id, story_id, "abandon_dag", by_uid)) is not None:
        return _frozen
    if not force:
        story = await pool.fetchrow("SELECT shca_instance_id FROM stories WHERE id = $1", story_id)
        has_changes, stat = await workspace_has_changes(
            (story and story["shca_instance_id"]) or "", story_id)
        if has_changes:
            return {
                "ok": False, "needs_confirm": True, "action": "abandon",
                "story_id": story_id, "dag_task_id": dag["id"], "stat": stat,
                "reason": (
                    f"Completed-phase work is uncommitted and will be DISCARDED (never "
                    f"pushed){(': ' + stat) if stat else ''}. Abandon anyway, or use "
                    f"'Review & keep' to push it first."
                ),
            }
    await _close_dag_and_children(pool, dag["id"], dag_state="cancelled")
    await pool.execute(
        "UPDATE stories SET current_phase = 'awaiting_next', updated_at = NOW() WHERE id = $1",
        story_id,
    )
    await _post(goal_id, story_id, f"{dag['id']}-abandon", "awaiting_next",
                f"✕ Abandoned DAG `{dag['id']}`. Any uncommitted work was discarded. The "
                f"story is open — start a new plan or mark it done.", "ABANDONED")
    await _emit(goal_id, "dag.abandoned", {"story_id": story_id, "dag_task_id": dag["id"], "by": by_uid})
    return {"ok": True, "story_id": story_id, "dag_task_id": dag["id"]}


async def review_and_keep_dag(story_id: str, by_uid: str = "") -> dict:
    """Salvage a partial DAG instead of discarding it: capture the current working-tree diff
    (what the completed phases produced) and route to the diff-review gate so the human can
    **Approve & Push** it. Cancels the non-done children (the failed/blocked phase), keeps
    `done`, moves the DAG + story to `awaiting_review`, and posts the diff card. Approve on
    that card commits+pushes and closes the story; Reject sends it back to Claude."""
    pool = get_pool()
    dag = await _current_dag(pool, story_id)
    if not dag:
        return {"ok": False, "error": "no DAG for story"}
    goal_id = dag["goal_id"]
    if (_frozen := await _refuse_if_frozen(goal_id, story_id, "review_and_keep_dag", by_uid)) is not None:
        return _frozen
    story = await pool.fetchrow("SELECT shca_instance_id FROM stories WHERE id = $1", story_id)
    instance_id = (story and story["shca_instance_id"]) or ""
    has_changes, _stat = await workspace_has_changes(instance_id, story_id)
    if not has_changes:
        return {"ok": False, "error": "no uncommitted changes to keep — nothing to review yet"}
    # Capture the diff + post the review card FIRST. begin_diff_review doesn't depend on the
    # DAG/story state, and doing it before we mutate state means a gitDiff failure returns
    # cleanly with nothing changed (no story stranded in awaiting_review with no card).
    from app.services.diff_review import begin_diff_review  # noqa: PLC0415
    result = await begin_diff_review(goal_id, story_id, dag["id"])
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "diff capture failed"}
    # Now flip state: cancel every non-done, non-terminal child (the failed/blocked phase +
    # any pending dependents) so the DAG reflects "review the completed phases' combined
    # output" (keep 'done'), and park the parent at 'awaiting_approval' — the SAME state the
    # normal all-done gate uses, so the rollup CAS guards in sentinel_parser /
    # task_controller can't re-enter and double-post a review. The STORY phase is
    # 'awaiting_review' (its normal value for the diff gate).
    await pool.execute(
        "UPDATE tasks SET state = 'cancelled', updated_at = NOW() "
        "WHERE parent_task_id = $1 AND executor = 'claude-code-phase' "
        "AND state NOT IN ('done', 'cancelled')",
        dag["id"],
    )
    await pool.execute(
        "UPDATE tasks SET state = 'awaiting_approval', updated_at = NOW() WHERE id = $1", dag["id"])
    await pool.execute(
        "UPDATE stories SET current_phase = 'awaiting_review', updated_at = NOW() WHERE id = $1",
        story_id,
    )
    await _emit(goal_id, "dag.review_and_keep",
                {"story_id": story_id, "dag_task_id": dag["id"], "by": by_uid})
    return {"ok": True, "story_id": story_id, "dag_task_id": dag["id"]}


async def reject_plan(story_id: str, feedback: str = "", by_uid: str = "") -> dict:
    """Reject a proposed plan (phase `awaiting_plan_approval`) → back to `planning` so the
    assistant revises it. Parity with diff-review reject; no DAG is ingested yet, so
    nothing to cancel. Closes the plan-reject dead-end (there was previously no endpoint)."""
    pool = get_pool()
    story = await pool.fetchrow("SELECT goal_id FROM stories WHERE id = $1", story_id)
    if not story:
        return {"ok": False, "error": "story not found"}
    goal_id = story["goal_id"]
    if (_frozen := await _refuse_if_frozen(goal_id, story_id, "reject_plan", by_uid)) is not None:
        return _frozen
    await pool.execute(
        # Plan rejected → clear the rejected plan's pointer so "Approve plan" is disabled
        # until the revised plan is written.
        "UPDATE stories SET current_phase = 'planning', current_plan_path = NULL, "
        "updated_at = NOW() WHERE id = $1",
        story_id,
    )
    body = "♻ Plan rejected — back at `planning` to revise."
    if feedback:
        body += f"\n\nHuman feedback:\n> {feedback}"
    await _post(goal_id, story_id, f"{story_id}-plan-reject", "planning", body, "PLAN_REJECTED")
    await _emit(goal_id, "plan.rejected", {"story_id": story_id, "feedback": feedback, "by": by_uid})
    return {"ok": True, "story_id": story_id}
