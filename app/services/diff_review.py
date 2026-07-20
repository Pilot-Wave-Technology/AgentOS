"""Diff-review flow at DAG completion (Stage 3-l).

After all phase subtasks of a DAG transition to `done`:
  1. Capture the workspace diff (HEAD-vs-staged + unstaged) via gateway
  2. Write the patch to `docs/diff.patch` in the workspace so it shows
     up in the file viewer
  3. Post a diff-review card to chat with Approve / Reject buttons
  4. On human approve → final commit + push of the implementation,
     story → done
  5. On reject → feedback returns to Claude as a new dispatch
     (essentially a synthetic phase task that says "revise per
     feedback, then re-emit PHASE_TASK_DONE for the DAG parent")
"""
from __future__ import annotations

import json
import logging
import uuid

from app.database import get_pool
from app.ledger.journal import append_event
from app.openclaw import gateway_admin, GatewayError


_log = logging.getLogger(__name__)
from app.ledger.actors import SYSTEM_ACTOR_ID  # one canonical, registered actor
from app.ledger.actors import human_actor, instance_actor


async def begin_diff_review(
    goal_id: str, story_id: str, dag_task_id: str,
) -> dict:
    """Called when sentinel_parser detects all DAG subtasks are done.

    Captures the diff (writing patch file in workspace) and posts the
    review card. Returns the diff summary so callers can log.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        """SELECT shca_instance_id, active_thread_id, workspace_path
             FROM stories WHERE id = $1""",
        story_id,
    )
    if not story or not story["shca_instance_id"]:
        return {"ok": False, "error": "story / instance missing"}

    instance_id = story["shca_instance_id"]
    thread_id = story["active_thread_id"]

    # Capture diff via gateway. Also write to docs/diff.patch so the
    # file viewer can render it.
    try:
        diff_result = await gateway_admin.call(
            "shca.workspace.gitDiff",
            {
                "agentId": instance_id,
                "storyId": story_id,
                "scope": "all",  # staged + unstaged
                "writeToPath": "docs/diff.patch",
            },
            timeout=60,
        )
    except GatewayError as exc:
        _log.warning("diff_review: gitDiff RPC failed (story=%s): %s", story_id, exc)
        return {"ok": False, "error": f"diff capture failed: {exc}"}

    diff_text = diff_result.get("diff") or ""
    stat = diff_result.get("stat_summary") or ""
    truncated = bool(diff_result.get("truncated"))

    # Persist diff review state. Reuse `approvals` table from Phase 2a.
    approval_id = uuid.uuid4()
    payload = {
        "dag_task_id": dag_task_id,
        "diff_path": "docs/diff.patch",
        "stat": stat,
        "truncated": truncated,
        "preview": diff_text[:2000],
    }
    await pool.execute(
        """INSERT INTO approvals (
               id, story_id, kind, payload, channels, proposed_at
           ) VALUES ($1, $2, 'diff', $3::JSONB, ARRAY['ui'], NOW())""",
        approval_id, story_id, json.dumps(payload),
    )

    # Write a chat card.
    if thread_id:
        card = {
            "kind": "diff_review",
            "approval_id": str(approval_id),
            "dag_task_id": dag_task_id,
            "stat": stat,
            "diff_path": "docs/diff.patch",
            "truncated": truncated,
        }
        body = (
            f"# Diff ready for review\n\n"
            f"All DAG tasks completed. {stat or 'See diff in file viewer.'}\n\n"
            f"Patch saved to **`docs/diff.patch`** — open it in the file "
            f"viewer to review.\n\n"
            f"Approve to commit + push the implementation. "
            f"Reject with feedback to send Claude back for revisions."
        )
        msg_id = str(uuid.uuid4())
        await pool.execute(
            """INSERT INTO messages (
                   id, thread_id, role, sender,
                   content, card_data, created_at
               ) VALUES ($1, $2, 'agent', 'task-controller',
                         $3, $4::JSONB, NOW())""",
            msg_id, thread_id, body, json.dumps(card),
        )

        # Deliver live via the durable fan-out so open chat tabs see the card
        # without a refresh — on ANY web instance. The old local-only broadcast
        # reached zero subscribers here: this runs in the task-controller path,
        # which usually has no browser session, so the card only ever appeared on
        # refresh. NOTIFY lets every web instance re-broadcast to its own subs;
        # the fan-out broadcaster rebuilds the diff_review frame from card_data +
        # the message body. Persistent row above means a refresh also shows it.
        try:
            from app.chat_module.repository import notify_new_message
            await notify_new_message(thread_id, msg_id)
        except Exception:
            _log.exception(
                "diff_review: fan-out notify failed (story=%s) — card is in "
                "DB, page refresh will show it", story_id,
            )

        # Relay to Slack as Block Kit card with Approve & Push / Reject buttons.
        try:
            from app.channels.slack.outbound import relay_card as _slack_relay_card
            await _slack_relay_card(goal_id, thread_id, card, body)
        except Exception:
            _log.exception(
                "diff_review: Slack card relay failed (story=%s) — card is in "
                "DB, human can still approve via web UI", story_id,
            )

    # Ledger. The proposed diff is the SHCA's work → attribute to that instance.
    _dp_actor, _dp_type = await instance_actor(task_id=dag_task_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                actor_id=_dp_actor, actor_type=_dp_type,
                event_type="diff.proposed",
                payload={
                    "story_id": story_id,
                    "dag_task_id": dag_task_id,
                    "approval_id": str(approval_id),
                    "stat": stat,
                    "diff_truncated": truncated,
                },
                context_goal_id=goal_id,
            )

    return {
        "ok": True,
        "approval_id": str(approval_id),
        "stat": stat,
        "truncated": truncated,
    }


async def decide_diff_review(
    *, approval_id: str, decision: str, feedback: str,
    decided_by_uid: str,
) -> dict:
    """Human approves or rejects the diff.

    On 'approve': commit + push everything, mark story done.
    On 'reject': feedback is forwarded to Claude as a synthetic dispatch
        on the DAG parent (new phase=coder task to revise per feedback).
    """
    pool = get_pool()
    approval = await pool.fetchrow(
        "SELECT a.*, s.goal_id FROM approvals a "
        "JOIN stories s ON s.id = a.story_id WHERE a.id = $1",
        approval_id,
    )
    if not approval:
        return {"ok": False, "error": "approval not found"}
    if approval["decided_at"] is not None:
        return {"ok": False, "error": f"approval already {approval['decision']}"}
    if approval["kind"] != "diff":
        return {"ok": False, "error": f"approval kind mismatch: {approval['kind']}"}

    goal_id = approval["goal_id"]
    story_id = approval["story_id"]

    # Kill switch — approve commits+pushes and closes the story; reject dispatches
    # a revision task. Both would bypass a system/goal freeze. Shared with the web
    # "Approve/Reject diff" buttons and the Slack diff_approve/diff_reject buttons +
    # reject modal, so one check covers all of them. dispatch_block fails closed.
    from app.services import execution_control  # noqa: PLC0415
    _frozen = await execution_control.frozen_response(goal_id)
    if _frozen is not None:
        _log.info(
            "decide_diff_review refused — execution paused, goal=%s story=%s "
            "approval=%s uid=%s", goal_id, story_id, approval_id, decided_by_uid,
        )
        return _frozen

    payload = approval["payload"] if isinstance(approval["payload"], dict) else json.loads(approval["payload"])
    dag_task_id = payload.get("dag_task_id")
    # The diff decision is a human action → attribute to the deciding user.
    dr_actor, dr_type = await human_actor(decided_by_uid)

    story = await pool.fetchrow(
        """SELECT shca_instance_id, current_branch_name
             FROM stories WHERE id = $1""",
        story_id,
    )
    if not story:
        return {"ok": False, "error": "story not found"}
    instance_id = story["shca_instance_id"]

    if decision == "approve":
        # Refresh GitHub token inline so the credential helper holds a
        # fresh password right before the final push.
        from app.services.token_refresh import refresh_story_token
        refreshed, refresh_err = await refresh_story_token(story_id)
        if not refreshed:
            _log.info(
                "diff_review: pre-push token refresh skipped/failed "
                "(story=%s): %s — attempting push with existing credentials",
                story_id, refresh_err,
            )

        # Final commit + push.
        try:
            commit_result = await gateway_admin.call(
                "shca.workspace.gitCommit",
                {
                    "agentId": instance_id,
                    "storyId": story_id,
                    "paths": ["all"],
                    "message": f"impl: complete DAG {dag_task_id}",
                    "push": True,
                },
                timeout=120,
            )
        except GatewayError as exc:
            return {"ok": False, "error": f"commit RPC failed: {exc}"}

        push_error = commit_result.get("push_error")
        commit_sha = commit_result.get("commit_sha") or ""
        pushed = bool(commit_result.get("pushed"))

        if pushed:
            # Mark the approval decided ONLY on a successful push. On push failure we
            # deliberately leave the approval PENDING + the card actionable so the human
            # can re-click Approve to RETRY the push — the commit already landed, and
            # gitCommit re-pushes the existing commit on nothing-to-commit. (Previously
            # the card was marked decided before the push result was checked, so a failed
            # push consumed the card and stranded the story in awaiting_review with no
            # retry path and no surfaced error.)
            await pool.execute(
                """UPDATE approvals SET
                       decision = 'approved',
                       decided_at = NOW(),
                       decided_by = $1
                    WHERE id = $2""",
                decided_by_uid, approval_id,
            )
            await _mark_card_decided(approval_id, "approve")
            # Phase 4 Stage 5: DAG is done, but the STORY is not. After
            # diff approve + push, the story stays in_progress and the
            # phase transitions to 'awaiting_next' so the human can
            # either start another DAG (more features / fixes) or
            # explicitly mark the story done. Marking done is a
            # separate human action — never automatic.
            await pool.execute(
                """UPDATE tasks SET state='done', completed_at=NOW(),
                       updated_at=NOW() WHERE id = $1""",
                dag_task_id,
            )
            await pool.execute(
                """UPDATE stories SET
                       current_phase='awaiting_next',
                       updated_at=NOW()
                    WHERE id = $1""",
                story_id,
            )
            try:
                from app.services.go_events import recalculate_goal_completion
                await recalculate_goal_completion(goal_id)
            except Exception as _exc:
                _log.warning("recalculate_goal_completion failed after DAG done: %s", _exc)
            await _emit_ledger(goal_id, "diff.approved_and_pushed", {
                "story_id": story_id, "dag_task_id": dag_task_id,
                "commit_sha": commit_sha,
            }, actor_id=dr_actor, actor_type=dr_type)

            # Write a task-controller message announcing the push and
            # asking the human what's next. SHCA gets this via the
            # gateway-forward path (sentinel_parser plumbing).
            await _post_awaiting_next_prompt(
                goal_id=goal_id, story_id=story_id,
                commit_sha=commit_sha, dag_task_id=dag_task_id,
            )

            return {
                "ok": True,
                "decision": "approve",
                "pushed": True,
                "commit_sha": commit_sha,
            }
        else:
            # Commit landed but push FAILED. Approval left PENDING (card still actionable
            # above) so the human can re-click Approve to retry the push. Return ok=False
            # so the route surfaces the real error instead of a misleading "approved".
            await _emit_ledger(goal_id, "diff.approved_but_push_failed", {
                "story_id": story_id, "dag_task_id": dag_task_id,
                "commit_sha": commit_sha, "push_error": push_error,
            }, actor_id=dr_actor, actor_type=dr_type)
            return {
                "ok": False,
                "error": (
                    f"Commit succeeded but the push to the feature branch failed: "
                    f"{push_error or 'unknown error'}. Click Approve again to retry the "
                    f"push (the commit is already saved)."
                ),
                "decision": "approve",
                "pushed": False,
                "commit_sha": commit_sha,
                "push_error": push_error,
            }

    if decision == "reject":
        # 1. Record the rejection on the approval row.
        await pool.execute(
            """UPDATE approvals SET
                   decision = 'rejected',
                   decided_at = NOW(),
                   decided_by = $1,
                   feedback = $2
                WHERE id = $3""",
            decided_by_uid, feedback, approval_id,
        )
        await _mark_card_decided(approval_id, "reject")

        # 2. Close out the DAG. It's terminal in `rejected` — a new DAG
        #    will be created when SHCA's planning skill (MODE C) gets
        #    the next plan approved.
        await pool.execute(
            "UPDATE tasks SET state = 'rejected', updated_at = NOW() "
            "WHERE id = $1",
            dag_task_id,
        )
        # DAG tasks are umbrella records, not leaf executions, so the
        # handle is usually absent — but staying uniform across cancel
        # paths avoids a future bug if dag-level credentials ever land.
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        await release_credentials_safe(dag_task_id, "diff_review:dag_rejected")

        # 3. Send the story back to planning. The state snapshot built
        #    on SHCA's next turn will reflect phase='planning' and the
        #    unified assistant_code_planning skill will pick MODE C (NEW
        #    CYCLE) because the DAG has executed.
        await pool.execute(
            "UPDATE stories SET current_phase = 'planning', "
            "current_plan_path = NULL, "  # new cycle — keep Approve disabled until the new plan is written
            "updated_at = NOW() WHERE id = $1",
            story_id,
        )

        # 4. Post a task-controller message so SHCA sees the rejection
        #    + the human's feedback in chat. SHCA's planning skill
        #    reads this to brief Claude for the new cycle.
        try:
            from app.services.sentinel_parser import write_task_progress_card
            body = (
                f"♻ Diff review rejected. DAG `{dag_task_id}` is closed; "
                f"story is back at `planning` for a new cycle.\n\n"
                f"Human feedback:\n> {feedback}"
            )
            await write_task_progress_card(
                goal_id=goal_id,
                story_id=story_id,
                task_id=f"{dag_task_id}-reject",
                phase="planning",
                kind="output",
                text=body,
                sentinel_kind="DIFF_REJECTED",
            )
        except Exception:
            _log.exception(
                "diff_review reject: failed to post task-controller message "
                "(story=%s dag=%s)", story_id, dag_task_id,
            )

        await _emit_ledger(goal_id, "diff.rejected", {
            "story_id": story_id, "dag_task_id": dag_task_id,
            "feedback": feedback,
        }, actor_id=dr_actor, actor_type=dr_type)
        return {"ok": True, "decision": "reject"}

    return {"ok": False, "error": f"unknown decision {decision!r}"}


async def _post_awaiting_next_prompt(
    *, goal_id: str, story_id: str, commit_sha: str, dag_task_id: str,
) -> None:
    """After diff approve + push, write a task-controller message
    asking what's next. Story stays in_progress; phase=awaiting_next.

    Uses the same plumbing as sentinel_parser.write_task_progress_card
    so SHCA sees the message in real-time via the gateway-forward.
    """
    try:
        from app.services.sentinel_parser import write_task_progress_card
        body = (
            f"Diff approved and pushed (commit `{(commit_sha or '')[:12]}`). "
            f"DAG `{dag_task_id}` is complete. "
            f"Story is open for the next plan, or click ✓ Mark story done "
            f"when there's nothing more to add."
        )
        await write_task_progress_card(
            goal_id=goal_id,
            story_id=story_id,
            task_id=f"{dag_task_id}-await",
            phase="awaiting_next",
            kind="awaiting_next_prompt",
            text=body,
            sentinel_kind="AWAITING_NEXT",
        )
    except Exception:
        _log.exception(
            "diff_review: failed to post awaiting_next prompt "
            "(story=%s) — phase is set but human may not see the prompt",
            story_id,
        )


async def _mark_card_decided(approval_id: str, decision: str) -> None:
    """Stamp the chat message's card_data with `decided` so the template
    knows to hide the Approve/Reject buttons after page reload."""
    pool = get_pool()
    try:
        await pool.execute(
            """UPDATE messages
                  SET card_data = card_data || jsonb_build_object(
                          'decided', $1::text,
                          'decided_at', to_jsonb(NOW())
                      )
                WHERE card_data->>'kind' = 'diff_review'
                  AND card_data->>'approval_id' = $2""",
            decision, approval_id,
        )
    except Exception:
        _log.exception(
            "diff_review: failed to mark card decided (approval=%s) — "
            "approval row was still updated, this just keeps the chat "
            "card buttons live on next reload", approval_id,
        )


async def _emit_ledger(goal_id: str, event_type: str, payload: dict, *,
                       actor_id=SYSTEM_ACTOR_ID, actor_type="system") -> None:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    actor_id=actor_id, actor_type=actor_type,
                    event_type=event_type,
                    payload=payload,
                    context_goal_id=goal_id,
                )
    except Exception:
        _log.exception("ledger emit failed: %s", event_type)
