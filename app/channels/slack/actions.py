"""Handle Slack interactive actions (button clicks + modal submissions).

Called from channels/router.py after signature verification.

Flow for button clicks (`payload.type == "block_actions"`):
  - Parse the Block Kit action_id (see blocks.py action_id conventions)
  - For "Request Changes" buttons: open a modal via views.open so the user
    can type their feedback. Don't dispatch to the card handler yet.
  - For all other buttons: resolve the Slack user, dispatch to the shared
    card-response handler, update the original Slack message via response_url.

Flow for modal submits (`payload.type == "view_submission"`):
  - Parse the feedback text from view.state.values
  - Dispatch to the shared card-response handler with the real feedback
  - Update the original card message via bot_token (we have channel_id +
    message_ts stashed in the modal's private_metadata)

Return value: an optional dict. Slack's interactive endpoint interprets:
  - `None`   → default {"ok": true}, modal closes / button ack
  - `{}`     → modal closes (same as None)
  - `{"response_action": "clear"}` → clears entire modal stack
  - `{"response_action": "errors", ...}` → shows validation errors

No imports from app.slack.* — fully self-contained in the new channel system.
"""
import json
import logging
import re
from typing import Optional

import httpx

from app.channels.slack.blocks import (
    render_card_as_blocks,
    responded_footer_block,
    strip_response_buttons,
)
from app.channels.slack.thread_map import auto_map_by_email, get_user_uid

_log = logging.getLogger(__name__)

_ACTION_RE = re.compile(
    r"^card_(approve|changes|replan|retry|cancel|reply|details)_([0-9a-f-]+)(?:_(.+))?$"
)

# ctrl_pause_{story_id} / ctrl_replan_{story_id} / ctrl_approve_plan_{story_id}
_CTRL_ACTION_RE = re.compile(r"^ctrl_(pause|replan|approve_plan)_([0-9a-f-]+)$")

# diff_approve_{approval_id} / diff_reject_{approval_id} / diff_details_{approval_id}
_DIFF_ACTION_RE = re.compile(r"^diff_(approve|reject|details)_([0-9a-f-]+)$")

# recover_{replan|retry|review_keep|abandon}_{story_id}
_RECOVER_ACTION_RE = re.compile(r"^recover_(replan|retry|review_keep|abandon)_([0-9a-f-]+)$")

_CHANGES_MODAL_CALLBACK_ID = "card_changes_modal"
_DETAILS_MODAL_CALLBACK_ID = "card_details_modal"
_DIFF_REJECT_MODAL_CALLBACK_ID = "diff_reject_modal"

# Above this size, render details as an uploaded file snippet instead of a modal.
_DETAILS_MODAL_MAX_CHARS = 30000


async def handle_slack_action(payload: dict) -> Optional[dict]:
    """Entry point from router.py. Never raises — router always returns 200."""
    try:
        return await _dispatch(payload)
    except Exception as exc:
        _log.error("Slack action dispatch error: %s", exc, exc_info=True)
        return None


async def _dispatch(payload: dict) -> Optional[dict]:
    ptype = payload.get("type", "")

    if ptype == "view_submission":
        return await _handle_view_submission(payload)

    if ptype == "block_actions":
        return await _handle_block_actions(payload)

    _log.info("Ignoring Slack interaction type=%s", ptype)
    return None


# ── Button click path ────────────────────────────────────────────────────────

async def _handle_block_actions(payload: dict) -> Optional[dict]:
    actions = payload.get("actions") or []
    if not actions:
        return None

    action = actions[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "") or ""
    team = payload.get("team") or {}
    team_id = team.get("id", "")
    user = payload.get("user") or {}
    slack_user_id = user.get("id", "")
    response_url = payload.get("response_url", "")
    channel = payload.get("channel") or {}
    channel_id = channel.get("id", "")
    message = payload.get("message") or {}
    message_ts = message.get("ts", "")
    trigger_id = payload.get("trigger_id", "")

    _log.info(
        "Slack block_action: action_id=%s value=%r user=%s team=%s",
        action_id, value[:80], slack_user_id, team_id,
    )

    from app.database import get_pool
    pool = get_pool()

    # Handle story-level control buttons (pause / replan) before card actions.
    ctrl_m = _CTRL_ACTION_RE.match(action_id)
    if ctrl_m:
        ctrl_kind = ctrl_m.group(1)
        story_id = ctrl_m.group(2)
        user_uid = await _resolve_user(slack_user_id, team_id, pool, channel_id)
        if not user_uid:
            await _respond_ephemeral(
                response_url,
                "I don't recognize your account. Sign in to the control panel first.",
            )
            return None
        await _handle_story_control(
            ctrl_kind=ctrl_kind,
            story_id=story_id,
            user_uid=user_uid,
            response_url=response_url,
        )
        return None

    diff_m = _DIFF_ACTION_RE.match(action_id)
    if diff_m:
        diff_kind = diff_m.group(1)
        approval_id = diff_m.group(2)
        user_uid = await _resolve_user(slack_user_id, team_id, pool, channel_id)
        if not user_uid:
            await _respond_ephemeral(
                response_url,
                "I don't recognize your account. Sign in to the control panel first.",
            )
            return None

        if diff_kind == "reject":
            ok = await _open_diff_reject_modal(
                team_id=team_id, pool=pool, trigger_id=trigger_id,
                approval_id=approval_id, channel_id=channel_id,
                message_ts=message_ts,
                slack_user_label=user.get("name") or user.get("username") or slack_user_id,
            )
            if not ok:
                await _respond_ephemeral(
                    response_url,
                    "Could not open feedback modal. Reply in this channel with your "
                    "feedback and the assistant will pick it up.",
                )
            return None

        if diff_kind == "details":
            await _handle_diff_details(
                team_id=team_id, pool=pool, trigger_id=trigger_id,
                approval_id=approval_id, channel_id=channel_id,
                message_ts=message_ts, response_url=response_url,
            )
            return None

        if diff_kind == "approve":
            await _dispatch_diff_response(
                approval_id=approval_id, decision="approve", feedback="",
                user_uid=user_uid,
                user_label=user.get("name") or user.get("username") or slack_user_id,
                response_url=response_url, original_message=message,
            )
            return None

        return None

    # DAG recovery buttons (Phase 3): replan / retry / review_keep / abandon.
    recover_m = _RECOVER_ACTION_RE.match(action_id)
    if recover_m:
        recover_kind = recover_m.group(1)
        story_id = recover_m.group(2)
        user_uid = await _resolve_user(slack_user_id, team_id, pool, channel_id)
        if not user_uid:
            await _respond_ephemeral(
                response_url,
                "I don't recognize your account. Sign in to the control panel first.",
            )
            return None
        # Authorize: the resolved user must be a member of the story's goal.
        goal_id = await pool.fetchval("SELECT goal_id FROM stories WHERE id = $1", story_id)
        from app.auth import get_goal_role
        if not goal_id or await get_goal_role(user_uid, goal_id) is None:
            await _respond_ephemeral(response_url, "You don't have access to this story's goal.")
            return None
        from app.services import story_recovery as _sr
        if recover_kind == "replan":
            result = await _sr.replan_dag(story_id, by_uid=user_uid)
        elif recover_kind == "retry":
            result = await _sr.retry_dag(story_id, by_uid=user_uid)
        elif recover_kind == "review_keep":
            result = await _sr.review_and_keep_dag(story_id, by_uid=user_uid)
        else:
            result = await _sr.abandon_dag(story_id, by_uid=user_uid)
        # Abandon would discard uncommitted completed-phase work. Slack has no inline
        # confirm dialog, so don't silently discard — surface the warning and point at
        # "Review & keep" (or the web UI to force-discard).
        if result.get("needs_confirm"):
            await _respond_ephemeral(
                response_url,
                (result.get("reason") or "Uncommitted work would be discarded.")
                + " Tap 📥 Review & keep to save it, or abandon from the web UI to discard.",
            )
            return None
        if result.get("ok"):
            try:
                from app.task_controller import trigger as tc_trigger
                await tc_trigger()
            except Exception:
                pass
            if result.get("dag_task_id"):
                await _sr.mark_recovery_cards_decided(result["dag_task_id"], recover_kind)
            await _respond_ephemeral(response_url, f"{recover_kind} started.")
        else:
            await _respond_ephemeral(
                response_url,
                f"{recover_kind} failed: {result.get('user_message') or result.get('error', 'error')}")
        return None

    m = _ACTION_RE.match(action_id)
    if not m:
        _log.info("Unrecognized action_id: %s", action_id)
        return None

    kind = m.group(1)
    task_id = m.group(2)
    extra = m.group(3) or ""

    user_uid = await _resolve_user(slack_user_id, team_id, pool, channel_id)
    if not user_uid:
        await _respond_ephemeral(
            response_url,
            "I don't recognize your account. Sign in to the control panel with "
            "the same email as your Slack account, then try again.",
        )
        return None

    if kind == "changes":
        ok = await _open_changes_modal(
            team_id=team_id,
            pool=pool,
            trigger_id=trigger_id,
            task_id=task_id,
            channel_id=channel_id,
            message_ts=message_ts,
            original_text=message.get("text", "") or "",
            slack_user_label=user.get("name") or user.get("username") or slack_user_id,
        )
        if not ok:
            await _respond_ephemeral(
                response_url,
                "Could not open the feedback modal. Please reply in this channel "
                "with your desired changes and the planner will pick it up.",
            )
        return None

    if kind == "details":
        await _handle_view_details(
            team_id=team_id,
            pool=pool,
            trigger_id=trigger_id,
            task_id=task_id,
            card_type=extra,
            channel_id=channel_id,
            message_ts=message_ts,
            response_url=response_url,
        )
        return None

    option_id, value = _map_button(kind, extra, value)
    await _dispatch_card_response(
        task_id=task_id,
        option_id=option_id,
        value=value,
        user_uid=user_uid,
        kind=kind,
        user_label=user.get("name") or user.get("username") or slack_user_id,
        response_url=response_url,
        original_message=message,
    )
    return None


async def _handle_story_control(
    ctrl_kind: str,
    story_id: str,
    user_uid: str,
    response_url: str,
) -> None:
    """Handle pause / replan / approve_plan story-control button clicks from Slack."""
    try:
        from app.database import get_pool
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT goal_id, title, current_phase, execution_paused FROM stories WHERE id = $1",
            story_id,
        )
        if not row:
            await _respond_ephemeral(response_url, f"Story `{story_id[:8]}` not found.")
            return
        goal_id = str(row["goal_id"])
        story_title = row["title"] or story_id[:8]

        # Kill switch — pause/replan below mutate story/task rows directly (no
        # shared service to gate centrally), so check here. approve_plan is
        # already gated inside app.services.plan_approval itself.
        from app.services import execution_control
        _frozen = await execution_control.frozen_response(goal_id)
        if ctrl_kind in ("pause", "replan") and _frozen is not None:
            _log.info(
                "Story control via Slack refused — execution paused: kind=%s goal=%s story=%s user=%s",
                ctrl_kind, goal_id, story_id[:8], user_uid,
            )
            await _respond_ephemeral(response_url, _frozen["user_message"])
            return

        if ctrl_kind == "approve_plan":
            from app.services.plan_approval import approve_plan
            result = await approve_plan(
                goal_id=goal_id,
                story_id=story_id,
                approved_by_uid=user_uid,
            )
            if result.get("ok"):
                task_count = result.get("task_count", "?")
                await _respond_ephemeral(
                    response_url,
                    f"✅ Plan approved for *{story_title}*. {task_count} tasks dispatched.",
                )
            else:
                await _respond_ephemeral(
                    response_url,
                    f"❌ Plan approval failed: "
                    f"{result.get('user_message') or result.get('error', 'unknown error')}. "
                    "Try approving from the control panel.",
                )
            _log.info(
                "Plan approve via Slack: goal=%s story=%s user=%s ok=%s",
                goal_id, story_id[:8], user_uid, result.get("ok"),
            )
            return

        if ctrl_kind == "pause":
            if row["execution_paused"]:
                await _respond_ephemeral(response_url, f"⏸ Story *{story_title}* is already paused.")
                return
            await pool.execute(
                "UPDATE stories SET execution_paused = TRUE, updated_at = NOW() WHERE id = $1",
                story_id,
            )
            await _respond_ephemeral(
                response_url,
                f"⏸ Story *{story_title}* paused. No new tasks will dispatch. "
                "Resume or replan from the control panel.",
            )
            _log.info("Story pause via Slack: goal=%s story=%s user=%s", goal_id, story_id[:8], user_uid)

        elif ctrl_kind == "replan":
            await pool.execute(
                """UPDATE tasks
                      SET state = 'cancelled', updated_at = NOW()
                    WHERE story_id = $1
                      AND executor = 'claude-code-phase'
                      AND state IN ('pending', 'ready')""",
                story_id,
            )
            await pool.execute(
                """UPDATE stories
                      SET current_phase = 'planning',
                          current_plan_path = NULL,
                          execution_paused = FALSE,
                          updated_at = NOW()
                    WHERE id = $1""",
                story_id,
            )
            await _respond_ephemeral(
                response_url,
                f"🔄 Replan started for *{story_title}*. Pending tasks cancelled. "
                "Assistant will revise the plan — tell it what to change.",
            )
            _log.info("Story replan via Slack: goal=%s story=%s user=%s", goal_id, story_id[:8], user_uid)

    except Exception as exc:
        _log.warning(
            "Story control action failed: kind=%s story=%s: %s", ctrl_kind, story_id[:8], exc,
        )
        await _respond_ephemeral(
            response_url,
            f"Action failed: {exc}. Use the control panel instead.",
        )


def _map_button(kind: str, extra: str, value: str) -> tuple[str, str]:
    """Map (action kind, extra, value) → (option_id, value) for handle_card_response."""
    if kind == "approve":
        return "approve", value or "approve"
    if kind == "replan":
        return "replan", value or "replan"
    if kind == "retry":
        return "retry", value or "retry"
    if kind == "cancel":
        return "cancel", value or "cancel"
    if kind == "reply":
        return extra or "reply", value
    return kind, value


# ── Modal open ───────────────────────────────────────────────────────────────

async def _open_changes_modal(
    team_id: str,
    pool,
    trigger_id: str,
    task_id: str,
    channel_id: str,
    message_ts: str,
    original_text: str,
    slack_user_label: str,
) -> bool:
    if not trigger_id:
        return False

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from app.channels.slack.config import get_workspace
    except ImportError:
        return False

    ws = await get_workspace(team_id, pool)
    if not ws or not ws.get("bot_token"):
        return False

    private_metadata = json.dumps({
        "task_id": task_id,
        "channel_id": channel_id,
        "message_ts": message_ts,
        "user_label": slack_user_label,
        "original_text": original_text[:2000],
    })

    view = {
        "type": "modal",
        "callback_id": _CHANGES_MODAL_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Request Changes"},
        "submit": {"type": "plain_text", "text": "Send feedback"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Tell the planner what you'd like changed."},
            },
            {
                "type": "input",
                "block_id": "feedback",
                "label": {"type": "plain_text", "text": "Your feedback"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "text",
                    "multiline": True,
                    "min_length": 5,
                    "max_length": 2000,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "E.g. 'Skip the deploy step; we'll handle it manually.'",
                    },
                },
            },
        ],
    }

    try:
        client = WebClient(token=ws["bot_token"])
        client.views_open(trigger_id=trigger_id, view=view)
        _log.info("Opened Request Changes modal: task=%s", task_id)
        return True
    except SlackApiError as exc:
        _log.warning("views.open failed (task=%s): %s", task_id, exc.response.get("error", str(exc)))
        return False
    except Exception as exc:
        _log.warning("views.open error (task=%s): %s", task_id, exc)
        return False


# ── Modal submission path ────────────────────────────────────────────────────

async def _handle_view_submission(payload: dict) -> Optional[dict]:
    view = payload.get("view") or {}
    callback_id = view.get("callback_id", "")

    if callback_id == _DIFF_REJECT_MODAL_CALLBACK_ID:
        return await _handle_diff_reject_submission(payload)

    if callback_id != _CHANGES_MODAL_CALLBACK_ID:
        _log.info("Ignoring view_submission with unknown callback_id=%s", callback_id)
        return None

    try:
        meta = json.loads(view.get("private_metadata", "") or "{}")
    except json.JSONDecodeError:
        meta = {}
    task_id = meta.get("task_id", "")
    channel_id = meta.get("channel_id", "")
    message_ts = meta.get("message_ts", "")
    user_label = meta.get("user_label", "")
    original_text = meta.get("original_text", "") or ""

    state_values = view.get("state", {}).get("values", {})
    feedback = (
        state_values.get("feedback", {}).get("text", {}).get("value", "") or ""
    ).strip()

    if not feedback:
        return {
            "response_action": "errors",
            "errors": {"feedback": "Please write a few words of feedback."},
        }

    team = payload.get("team") or {}
    team_id = team.get("id", "")
    user = payload.get("user") or {}
    slack_user_id = user.get("id", "")

    from app.database import get_pool
    pool = get_pool()

    user_uid = await _resolve_user(slack_user_id, team_id, pool, channel_id)
    if not user_uid:
        return {
            "response_action": "errors",
            "errors": {"feedback": "Your Slack account isn't linked to the control panel yet."},
        }

    try:
        from app.services.card_responses import handle_card_response
        result = await handle_card_response(
            task_id=task_id,
            option_id="changes",
            value=feedback,
            user_uid=user_uid,
        )
    except Exception as exc:
        _log.error("handle_card_response raised in modal submit: %s", exc, exc_info=True)
        return {
            "response_action": "errors",
            "errors": {"feedback": f"Something went wrong: {exc}"},
        }

    if not result.ok:
        return {
            "response_action": "errors",
            "errors": {"feedback": result.error or "Couldn't process"},
        }

    if channel_id and message_ts:
        try:
            from slack_sdk import WebClient
            from app.channels.slack.config import get_workspace
            ws = await get_workspace(team_id, pool)
            if ws and ws.get("bot_token"):
                client = WebClient(token=ws["bot_token"])
                update_body = await _build_responded_update(
                    task_id=task_id,
                    original_message={"text": original_text, "blocks": []},
                    responder=user_label or slack_user_id,
                    action="Changes requested",
                    value=feedback,
                )
                client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=update_body["text"],
                    blocks=update_body["blocks"],
                )
        except Exception as exc:
            _log.warning("chat.update after modal submit failed: %s", exc)

    _log.info(
        "Changes modal dispatched: task=%s uid=%s feedback_len=%d",
        task_id, user_uid, len(feedback),
    )
    return None


# ── Shared dispatch for non-modal buttons ────────────────────────────────────

async def _dispatch_card_response(
    task_id: str,
    option_id: str,
    value: str,
    user_uid: str,
    kind: str,
    user_label: str,
    response_url: str,
    original_message: dict,
) -> None:
    try:
        from app.services.card_responses import handle_card_response
        result = await handle_card_response(
            task_id=task_id, option_id=option_id, value=value, user_uid=user_uid,
        )
    except Exception as exc:
        _log.error("handle_card_response raised: %s", exc, exc_info=True)
        await _respond_ephemeral(response_url, f"Something went wrong processing your response: {exc}")
        return

    if not result.ok:
        # Prefer the friendly user_message (e.g. the kill-switch "paused — resume
        # first" notice) over the raw error code.
        await _respond_ephemeral(
            response_url,
            result.user_message or f"Couldn't process: {result.error or 'unknown error'}",
        )
        return

    action_label = {
        "approved": "Plan approved",
        "changes_requested": "Changes requested",
        "replanned": "Re-plan requested",
        "answered": "Answered",
        "noop": "Acknowledged",
    }.get(result.action, result.action)

    shown_value = value if kind in ("reply", "replan") else ""

    update_body = await _build_responded_update(
        task_id=task_id,
        original_message=original_message,
        responder=user_label,
        action=action_label,
        value=shown_value,
    )
    update_body["replace_original"] = True

    if response_url:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(response_url, json=update_body)
        except Exception as exc:
            _log.warning("Failed to update Slack card via response_url: %s", exc)

    _log.info(
        "Card response dispatched: task=%s kind=%s action=%s uid=%s",
        task_id, kind, result.action, user_uid,
    )


# ── On-demand plan refetch ───────────────────────────────────────────────────

async def _refetch_plan_from_repo(task_id: str) -> str:
    """Fetch .plan/PLAN.md from the task's story branch on-demand and cache it.

    Returns the fetched content (empty string on any failure).
    """
    from app.database import get_pool
    pool = get_pool()

    story_row = await pool.fetchrow(
        "SELECT story_id, parent_task_id FROM tasks WHERE id = $1", task_id,
    )
    if not story_row:
        return ""
    story_id = story_row.get("story_id") or ""
    if not story_id and story_row.get("parent_task_id"):
        story_id = await pool.fetchval(
            "SELECT story_id FROM tasks WHERE id = $1", story_row["parent_task_id"],
        ) or ""
    if not story_id:
        _log.info("Plan refetch: task %s has no story_id linkage", task_id)
        return ""

    branch_resource = await pool.fetchrow("""
        SELECT gr.branch_name, gr.repo_name, gr.repo_owner
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1
           AND gr.kind = 'branch'
           AND sr.relation = 'produces'
         LIMIT 1
    """, story_id)
    if not branch_resource:
        _log.info("Plan refetch: no produces-branch resource for story %s", story_id)
        return ""

    repo_name = branch_resource.get("repo_name") or ""
    branch_name = branch_resource.get("branch_name") or ""
    if not repo_name or not branch_name:
        return ""

    try:
        from app.github_files import fetch_file_from_repo
    except ImportError:
        return ""

    from app.plan_paths import candidate_plan_paths
    candidate_paths = await candidate_plan_paths(task_id)

    for path in candidate_paths:
        content = await fetch_file_from_repo(repo_name, path, branch_name)
        if content and content.strip():
            try:
                await pool.execute(
                    "UPDATE tasks SET plan_content = $1 WHERE id = $2",
                    content, task_id,
                )
            except Exception as exc:
                _log.warning("Failed to cache refetched plan for %s: %s", task_id, exc)
            _log.info(
                "Refetched plan for task %s from %s/%s/%s (%d chars)",
                task_id, repo_name, branch_name[:30], path, len(content),
            )
            return content

    _log.info(
        "Plan refetch: %s not found in %s/%s (tried %s)",
        task_id, repo_name, branch_name[:30], ", ".join(candidate_paths),
    )
    return ""


# ── Post-response rewrite ────────────────────────────────────────────────────

async def _build_responded_update(
    task_id: str,
    original_message: dict,
    responder: str,
    action: str,
    value: str = "",
) -> dict:
    """Build the chat.update / response_url body preserving card content without buttons."""
    from app.database import get_pool

    blocks: list[dict] = []
    text_fallback = ""
    try:
        pool = get_pool()
        row = await pool.fetchrow("""
            SELECT card_data FROM messages
             WHERE card_data IS NOT NULL
               AND card_data->>'task_id' = $1
             ORDER BY created_at DESC
             LIMIT 1
        """, task_id)
        if row and row["card_data"]:
            card = row["card_data"]
            if isinstance(card, str):
                card = json.loads(card)
            blocks = render_card_as_blocks(card) or []
            text_fallback = card.get("title", "") or card.get("body", "")[:150]
    except Exception as exc:
        _log.warning("Failed to re-render card for task %s: %s", task_id, exc)

    if not blocks:
        blocks = list(original_message.get("blocks") or [])
        text_fallback = original_message.get("text", "") or "Card"

    stripped = strip_response_buttons(blocks)
    stripped.append(responded_footer_block(responder, action, value))

    return {
        "text": f"{text_fallback} — {action}".strip(),
        "blocks": stripped,
    }


# ── View Details ─────────────────────────────────────────────────────────────

async def _fetch_card_context(task_id: str, card_type: str = "") -> tuple[str, str, str]:
    """Look up context to show when the user clicks View Plan / View Details.

    For approval cards prefers the LIVE tasks.plan_content over frozen card_data.
    """
    from app.database import get_pool
    pool = get_pool()

    if not card_type:
        has_approval = await pool.fetchval("""
            SELECT 1 FROM messages
             WHERE card_data IS NOT NULL
               AND card_data->>'task_id' = $1
               AND card_data->>'card_type' = 'approval'
             LIMIT 1
        """, task_id)
        if has_approval:
            card_type = "approval"

    if card_type == "approval":
        live_plan = await pool.fetchval(
            "SELECT plan_content FROM tasks WHERE id = $1", task_id,
        )
        task_title = await pool.fetchval(
            "SELECT title FROM tasks WHERE id = $1", task_id,
        ) or "Plan"

        if live_plan and live_plan.strip():
            return live_plan, task_title, "approval"

        refetched = await _refetch_plan_from_repo(task_id)
        if refetched and refetched.strip():
            return refetched, task_title, "approval"

    row = None
    if card_type:
        row = await pool.fetchrow("""
            SELECT card_data FROM messages
             WHERE card_data IS NOT NULL
               AND card_data->>'task_id' = $1
               AND card_data->>'card_type' = $2
               AND coalesce(card_data->>'context', '') <> ''
             ORDER BY created_at DESC
             LIMIT 1
        """, task_id, card_type)

    if not row:
        row = await pool.fetchrow("""
            SELECT card_data FROM messages
             WHERE card_data IS NOT NULL
               AND card_data->>'task_id' = $1
               AND coalesce(card_data->>'context', '') <> ''
             ORDER BY created_at DESC
             LIMIT 1
        """, task_id)

    if not row:
        row = await pool.fetchrow("""
            SELECT card_data FROM messages
             WHERE card_data IS NOT NULL
               AND card_data->>'task_id' = $1
             ORDER BY created_at DESC
             LIMIT 1
        """, task_id)

    if not row or not row["card_data"]:
        return "", "", ""
    try:
        card = row["card_data"]
        if isinstance(card, str):
            card = json.loads(card)
    except Exception:
        return "", "", ""
    return (
        card.get("context", "") or "",
        card.get("title", "") or "Details",
        card.get("card_type", "") or "",
    )


async def _handle_view_details(
    team_id: str,
    pool,
    trigger_id: str,
    task_id: str,
    card_type: str,
    channel_id: str,
    message_ts: str,
    response_url: str,
) -> None:
    context, title, _ = await _fetch_card_context(task_id, card_type)
    if not context.strip():
        await _respond_ephemeral(response_url, "No details available for this card.")
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from app.channels.slack.config import get_workspace
    except ImportError:
        return

    ws = await get_workspace(team_id, pool)
    if not ws or not ws.get("bot_token"):
        return
    client = WebClient(token=ws["bot_token"])

    if len(context) > _DETAILS_MODAL_MAX_CHARS:
        filename = f"plan-{task_id[:8]}.md"
        try:
            client.files_upload_v2(
                channel=channel_id,
                thread_ts=message_ts,
                content=context,
                filename=filename,
                title=_truncate_slack(title, 80),
                initial_comment=f"Full details ({len(context):,} chars) — attached as a file.",
            )
            _log.info("Uploaded card details as file: task=%s chars=%d", task_id, len(context))
            await _respond_ephemeral(
                response_url,
                "The plan is large — I attached it as a file in this channel. "
                "Click the attachment to view or download.",
            )
        except SlackApiError as exc:
            _log.warning(
                "files_upload_v2 failed (task=%s): %s",
                task_id, exc.response.get("error", str(exc)),
            )
            await _respond_ephemeral(
                response_url,
                "Could not attach the plan file. Open the card in the control panel to view.",
            )
        return

    if not trigger_id:
        await _respond_ephemeral(response_url, "Could not open details modal (no trigger).")
        return

    try:
        client.views_open(
            trigger_id=trigger_id,
            view=_build_details_view(title, context, task_id),
        )
        _log.info("Opened details modal: task=%s chars=%d", task_id, len(context))
    except SlackApiError as exc:
        _log.warning(
            "views.open (details) failed (task=%s): %s",
            task_id, exc.response.get("error", str(exc)),
        )
        await _respond_ephemeral(
            response_url,
            "Could not open the details modal. Open the card in the control panel instead.",
        )


def _truncate_slack(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _build_details_view(title: str, context: str, task_id: str) -> dict:
    """Build a modal view showing `context`, split into sections to respect Slack limits."""
    from app.channels.slack.blocks import _split_text_for_blocks, _SEC_MAX

    header_title = _truncate_slack(title or "Details", 24)
    chunks = _split_text_for_blocks(context, _SEC_MAX - 10)
    blocks: list[dict] = []
    for chunk in chunks[:90]:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{chunk}```"},
        })
    if len(chunks) > 90:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_Showing first 90 sections of {len(chunks)}; "
                        f"request download via the control panel for the full content._",
            }],
        })
    return {
        "type": "modal",
        "callback_id": _DETAILS_MODAL_CALLBACK_ID,
        "title": {"type": "plain_text", "text": header_title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


# ── Diff review actions ───────────────────────────────────────────────────────

async def _dispatch_diff_response(
    approval_id: str,
    decision: str,
    feedback: str,
    user_uid: str,
    user_label: str,
    response_url: str,
    original_message: dict,
) -> None:
    try:
        from app.services.diff_review import decide_diff_review
        result = await decide_diff_review(
            approval_id=approval_id,
            decision=decision,
            feedback=feedback,
            decided_by_uid=user_uid,
        )
    except Exception as exc:
        _log.error("decide_diff_review raised: %s", exc, exc_info=True)
        await _respond_ephemeral(response_url, f"Something went wrong: {exc}")
        return

    if not result.get("ok"):
        await _respond_ephemeral(
            response_url,
            f"Couldn't process: {result.get('user_message') or result.get('error') or 'unknown error'}",
        )
        return

    if decision == "approve":
        pushed = result.get("pushed", False)
        commit_sha = (result.get("commit_sha") or "")[:12]
        if pushed:
            action_label = f"Approved & pushed (commit `{commit_sha}`)" if commit_sha else "Approved & pushed"
        else:
            push_err = result.get("push_error") or "push failed"
            action_label = f"Approved but push failed: {push_err}"
    else:
        action_label = "Rejected — sent back for revision"

    update_body = await _build_diff_responded_update(
        approval_id=approval_id,
        original_message=original_message,
        responder=user_label,
        action=action_label,
    )
    update_body["replace_original"] = True
    if response_url:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(response_url, json=update_body)
        except Exception as exc:
            _log.warning("Failed to update Slack diff card via response_url: %s", exc)

    _log.info(
        "Diff response dispatched: approval=%s decision=%s uid=%s",
        approval_id, decision, user_uid,
    )


async def _build_diff_responded_update(
    approval_id: str,
    original_message: dict,
    responder: str,
    action: str,
) -> dict:
    from app.database import get_pool
    pool = get_pool()
    blocks: list[dict] = []
    text_fallback = ""
    try:
        row = await pool.fetchrow("""
            SELECT card_data FROM messages
             WHERE card_data IS NOT NULL
               AND card_data->>'approval_id' = $1
             ORDER BY created_at DESC LIMIT 1
        """, approval_id)
        if row and row["card_data"]:
            card = row["card_data"]
            if isinstance(card, str):
                import json as _json
                card = _json.loads(card)
            blocks = render_card_as_blocks(card) or []
            text_fallback = card.get("stat", "Diff review")
    except Exception as exc:
        _log.warning("Failed to re-render diff card for approval %s: %s", approval_id, exc)

    if not blocks:
        blocks = list(original_message.get("blocks") or [])
        text_fallback = original_message.get("text", "") or "Diff review"

    stripped = strip_response_buttons(blocks)
    stripped.append(responded_footer_block(responder, action))
    return {
        "text": f"{text_fallback} — {action}".strip(),
        "blocks": stripped,
    }


async def _open_diff_reject_modal(
    team_id: str,
    pool,
    trigger_id: str,
    approval_id: str,
    channel_id: str,
    message_ts: str,
    slack_user_label: str,
) -> bool:
    if not trigger_id:
        return False

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from app.channels.slack.config import get_workspace
    except ImportError:
        return False

    ws = await get_workspace(team_id, pool)
    if not ws or not ws.get("bot_token"):
        return False

    private_metadata = json.dumps({
        "approval_id": approval_id,
        "channel_id": channel_id,
        "message_ts": message_ts,
        "user_label": slack_user_label,
    })

    view = {
        "type": "modal",
        "callback_id": _DIFF_REJECT_MODAL_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Reject Diff"},
        "submit": {"type": "plain_text", "text": "Send feedback"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": "Tell Claude what needs to change before this can be approved."},
            },
            {
                "type": "input",
                "block_id": "feedback",
                "label": {"type": "plain_text", "text": "Your feedback"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "text",
                    "multiline": True,
                    "min_length": 5,
                    "max_length": 2000,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "E.g. 'The tests are missing; add unit tests for the new handler.'",
                    },
                },
            },
        ],
    }

    try:
        client = WebClient(token=ws["bot_token"])
        client.views_open(trigger_id=trigger_id, view=view)
        _log.info("Opened diff reject modal: approval=%s", approval_id)
        return True
    except SlackApiError as exc:
        _log.warning(
            "views.open failed (diff reject, approval=%s): %s",
            approval_id, exc.response.get("error", str(exc)),
        )
        return False
    except Exception as exc:
        _log.warning("views.open error (diff reject, approval=%s): %s", approval_id, exc)
        return False


async def _handle_diff_reject_submission(payload: dict) -> Optional[dict]:
    view = payload.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata", "") or "{}")
    except json.JSONDecodeError:
        meta = {}

    approval_id = meta.get("approval_id", "")
    channel_id = meta.get("channel_id", "")
    message_ts = meta.get("message_ts", "")
    user_label = meta.get("user_label", "")

    state_values = view.get("state", {}).get("values", {})
    feedback = (
        state_values.get("feedback", {}).get("text", {}).get("value", "") or ""
    ).strip()

    if not feedback:
        return {
            "response_action": "errors",
            "errors": {"feedback": "Please write a few words of feedback."},
        }

    team = payload.get("team") or {}
    team_id = team.get("id", "")
    user = payload.get("user") or {}
    slack_user_id = user.get("id", "")

    from app.database import get_pool
    pool = get_pool()
    user_uid = await _resolve_user(slack_user_id, team_id, pool, channel_id)
    if not user_uid:
        return {
            "response_action": "errors",
            "errors": {"feedback": "Your Slack account isn't linked to the control panel yet."},
        }

    try:
        from app.services.diff_review import decide_diff_review
        result = await decide_diff_review(
            approval_id=approval_id,
            decision="reject",
            feedback=feedback,
            decided_by_uid=user_uid,
        )
    except Exception as exc:
        _log.error("decide_diff_review (reject) raised: %s", exc, exc_info=True)
        return {
            "response_action": "errors",
            "errors": {"feedback": f"Something went wrong: {exc}"},
        }

    if not result.get("ok"):
        return {
            "response_action": "errors",
            "errors": {"feedback": result.get("user_message") or result.get("error") or "Couldn't process"},
        }

    if channel_id and message_ts:
        try:
            from slack_sdk import WebClient
            from app.channels.slack.config import get_workspace
            ws = await get_workspace(team_id, pool)
            if ws and ws.get("bot_token"):
                client = WebClient(token=ws["bot_token"])
                update_body = await _build_diff_responded_update(
                    approval_id=approval_id,
                    original_message={},
                    responder=user_label or slack_user_id,
                    action="Rejected — sent back for revision",
                )
                client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=update_body["text"],
                    blocks=update_body["blocks"],
                )
        except Exception as exc:
            _log.warning("chat.update after diff reject failed: %s", exc)

    _log.info(
        "Diff reject dispatched: approval=%s uid=%s feedback_len=%d",
        approval_id, user_uid, len(feedback),
    )
    return None


async def _handle_diff_details(
    team_id: str,
    pool,
    trigger_id: str,
    approval_id: str,
    channel_id: str,
    message_ts: str,
    response_url: str,
) -> None:
    """Show the diff preview in a Slack modal (or upload as file if large)."""
    import json as _json
    row = await pool.fetchrow(
        "SELECT payload FROM approvals WHERE id = $1", approval_id,
    )
    if not row:
        await _respond_ephemeral(response_url, "Diff details not found.")
        return

    try:
        apayload = row["payload"] if isinstance(row["payload"], dict) else _json.loads(row["payload"])
    except Exception:
        apayload = {}

    preview = apayload.get("preview") or ""
    stat = apayload.get("stat") or "Diff"
    truncated = apayload.get("truncated", False)

    if not preview.strip():
        await _respond_ephemeral(response_url, "No diff preview available.")
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from app.channels.slack.config import get_workspace
    except ImportError:
        return

    ws = await get_workspace(team_id, pool)
    if not ws or not ws.get("bot_token"):
        return
    client = WebClient(token=ws["bot_token"])

    note = "\n\n_Diff was truncated — full patch available in the file viewer._" if truncated else ""
    content = preview + note

    if len(content) > _DETAILS_MODAL_MAX_CHARS:
        try:
            client.files_upload_v2(
                channel=channel_id,
                thread_ts=message_ts,
                content=content,
                filename="diff.patch",
                title=_truncate_slack(stat, 80),
                initial_comment="Diff preview (first 2000 chars shown).",
            )
            await _respond_ephemeral(response_url, "Diff attached as a file above.")
        except SlackApiError as exc:
            _log.warning("files_upload_v2 failed (diff, approval=%s): %s",
                         approval_id, exc.response.get("error", str(exc)))
            await _respond_ephemeral(response_url, "Could not attach diff file.")
        return

    if not trigger_id:
        await _respond_ephemeral(response_url, "Could not open diff modal (no trigger).")
        return

    try:
        client.views_open(
            trigger_id=trigger_id,
            view=_build_details_view(stat, content, approval_id),
        )
        _log.info("Opened diff details modal: approval=%s chars=%d", approval_id, len(content))
    except SlackApiError as exc:
        _log.warning(
            "views.open (diff details, approval=%s) failed: %s",
            approval_id, exc.response.get("error", str(exc)),
        )
        await _respond_ephemeral(response_url, "Could not open diff modal.")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_user(
    slack_user_id: str, team_id: str, pool, channel_id: str = "",
) -> str:
    if not slack_user_id or not team_id:
        return ""

    uid = await get_user_uid(slack_user_id, pool)
    if uid:
        return uid

    try:
        from slack_sdk import WebClient
        from app.channels.slack.config import get_workspace

        ws = await get_workspace(team_id, pool)
        if not ws or not ws.get("bot_token"):
            return ""
        client = WebClient(token=ws["bot_token"])
        info = client.users_info(user=slack_user_id)
        profile = (info.get("user") or {}).get("profile", {})
        email = profile.get("email", "")
        display_name = profile.get("display_name") or profile.get("real_name", "")
        if email:
            uid = await auto_map_by_email(slack_user_id, email, display_name, pool)
            if uid:
                return uid
    except Exception as exc:
        _log.warning("Failed to resolve Slack user %s: %s", slack_user_id, exc)

    return ""


async def _respond_ephemeral(response_url: str, text: str) -> None:
    if not response_url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(response_url, json={
                "response_type": "ephemeral",
                "replace_original": False,
                "text": text,
            })
    except Exception as exc:
        _log.warning("Failed to send ephemeral response: %s", exc)