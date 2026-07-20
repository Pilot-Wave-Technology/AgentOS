"""DEPRECATED — superseded by app/channels/slack/actions.py.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Handle Slack interactive actions (button clicks + modal submissions).

Called from `app/slack/router.py` after signature verification.

Flow for button clicks (`payload.type == "block_actions"`):
  - Parse the Block Kit action_id (see blocks.py action_id conventions)
  - For "Request Changes" buttons: open a modal via views.open so the user
    can type their feedback. Don't dispatch to the card handler yet.
  - For all other buttons: resolve the Slack user, dispatch to the shared
    card-response handler, update the original Slack message via
    response_url.

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
"""
import json
import logging
import re
from typing import Optional

import httpx

from app.slack.blocks import (
    render_card_as_blocks,
    responded_footer_block,
    strip_response_buttons,
)
from app.slack.thread_map import auto_map_user_by_email, get_user_uid

_log = logging.getLogger(__name__)

_ACTION_RE = re.compile(
    r"^card_(approve|changes|replan|retry|cancel|reply|details)_([0-9a-f-]+)(?:_(.+))?$"
)

# Modal callback_ids we use.
_CHANGES_MODAL_CALLBACK_ID = "card_changes_modal"
_DETAILS_MODAL_CALLBACK_ID = "card_details_modal"

# Above this size, render details as an uploaded file snippet instead of a modal.
_DETAILS_MODAL_MAX_CHARS = 30000


async def handle_slack_action(payload: dict) -> Optional[dict]:
    """Entry point from router.py. Never raises — router always returns 200.

    Returns None for the default ack; or a Slack-view response dict when a
    modal flow needs to signal something back (e.g. validation errors).
    """
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

    m = _ACTION_RE.match(action_id)
    if not m:
        _log.info("Unrecognized action_id: %s", action_id)
        return None

    kind = m.group(1)
    task_id = m.group(2)
    extra = m.group(3) or ""

    # Resolve the Slack user → control panel uid
    user_uid = await _resolve_user(slack_user_id, team_id, channel_id)
    if not user_uid:
        await _respond_ephemeral(
            response_url,
            "I don't recognize your account. Sign in to the control panel with "
            "the same email as your Slack account, then try again.",
        )
        return None

    # Request Changes → open modal, capture feedback, then dispatch on submit
    if kind == "changes":
        ok = await _open_changes_modal(
            team_id=team_id,
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

    # View Details → either open modal with context, or upload as a file
    if kind == "details":
        await _handle_view_details(
            team_id=team_id,
            trigger_id=trigger_id,
            task_id=task_id,
            card_type=extra,  # "approval" | "question" | "completion" | "failure" | ""
            channel_id=channel_id,
            message_ts=message_ts,
            response_url=response_url,
        )
        return None

    # All other buttons dispatch directly
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
    # "changes" is handled by modal, not here
    return kind, value


# ── Modal open ───────────────────────────────────────────────────────────────

async def _open_changes_modal(
    team_id: str,
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
        from app.slack.config import get_workspace
    except ImportError:
        return False

    ws = await get_workspace(team_id)
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
                "text": {
                    "type": "mrkdwn",
                    "text": "Tell the planner what you'd like changed.",
                },
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
        _log.warning(
            "views.open failed (task=%s): %s",
            task_id, exc.response.get("error", str(exc)),
        )
        return False
    except Exception as exc:
        _log.warning("views.open error (task=%s): %s", task_id, exc)
        return False


# ── Modal submission path ────────────────────────────────────────────────────

async def _handle_view_submission(payload: dict) -> Optional[dict]:
    view = payload.get("view") or {}
    callback_id = view.get("callback_id", "")

    if callback_id != _CHANGES_MODAL_CALLBACK_ID:
        _log.info("Ignoring view_submission with unknown callback_id=%s", callback_id)
        return None

    # Extract fields
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

    user_uid = await _resolve_user(slack_user_id, team_id, channel_id)
    if not user_uid:
        return {
            "response_action": "errors",
            "errors": {"feedback": "Your Slack account isn't linked to the control panel yet."},
        }

    # Dispatch to the shared handler (can take a few seconds; keep it
    # synchronous so the user sees validation / errors in the modal. Slack
    # gives us 3s — the happy path is typically well under that.)
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

    # Update the original card message via chat.update — we have channel + ts
    if channel_id and message_ts:
        try:
            from slack_sdk import WebClient
            from app.slack.config import get_workspace
            ws = await get_workspace(team_id)
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
    # Empty response closes the modal cleanly
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
        await _respond_ephemeral(
            response_url,
            f"Something went wrong processing your response: {exc}",
        )
        return

    if not result.ok:
        await _respond_ephemeral(
            response_url, f"Couldn't process: {result.error or 'unknown error'}",
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
    """If tasks.plan_content is empty, try to fetch .plan/PLAN.md from the
    task's story branch and cache the result in tasks.plan_content.

    Returns the fetched content (empty string on any failure). Uses the
    branch resource linked to the task's story via story_resources
    (kind='branch', relation='produces').
    """
    from app.database import get_pool
    pool = get_pool()

    # Resolve story_id — try task.story_id first, then parent_task_id's
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

    # Candidate paths resolved via the shared helper so this matches
    # what build_dispatch_message tells the IA and what routers/tasks.py
    # fetches on planner-completion. Namespaced path first, then legacy
    # fallbacks for in-flight chains that predate the namespacing.
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
    """Build the chat.update / response_url body that preserves the card's
    content (header, body, subtasks, artifacts, details button) but removes
    the response-action buttons and appends a responded-by footer.

    Falls back to whatever blocks were on the original message if the card
    can't be re-fetched from the DB — covers any edge case where card_data
    was deleted.
    """
    from app.database import get_pool

    # Prefer re-rendering from the stored card so we get the canonical layout
    # even if the Slack message blocks got munged by an earlier update.
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

    # Fallback to whatever blocks the interactive payload gave us
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

async def _fetch_card_context(
    task_id: str, card_type: str = "",
) -> tuple[str, str, str]:
    """Look up context to show when the user clicks View Plan / View Details.

    For approval cards we prefer the LIVE `tasks.plan_content` column over
    whatever was frozen into card_data at emit time. Reason: if the planner
    wrote .plan/PLAN.md after the card was posted (or our plan-fetcher ran
    later), plan_content holds the authoritative plan — the card's `context`
    may still be the raw planner completion text from the fallback path in
    task_controller.py.

    For other card types, falls back to the latest matching card_data.context,
    then latest card with any non-empty context, then latest card overall.
    """
    from app.database import get_pool
    pool = get_pool()

    # If we don't have a card_type from the action_id (old buttons rendered
    # before the _<card_type> suffix was added), try to infer it from the DB.
    # Prefer 'approval' if any approval card exists for this task — that's the
    # only card type that uses a repo-backed plan, so the approval path is the
    # one the user typically wants.
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

    # 1. For approval cards: prefer live tasks.plan_content; if empty, try
    # to fetch .plan/PLAN.md from the task's story branch on demand and
    # cache it so next click is instant.
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

    # 2. Latest card_data matching (task_id, card_type) with non-empty context
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

    # 3. Latest card for this task with any non-empty context
    if not row:
        row = await pool.fetchrow("""
            SELECT card_data FROM messages
             WHERE card_data IS NOT NULL
               AND card_data->>'task_id' = $1
               AND coalesce(card_data->>'context', '') <> ''
             ORDER BY created_at DESC
             LIMIT 1
        """, task_id)

    # 4. Last resort: latest card of any kind (may have empty context)
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
        from app.slack.config import get_workspace
    except ImportError:
        return

    ws = await get_workspace(team_id)
    if not ws or not ws.get("bot_token"):
        return
    client = WebClient(token=ws["bot_token"])

    # Large plans → upload as a snippet file in the channel thread
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
            _log.info(
                "Uploaded card details as file: task=%s chars=%d",
                task_id, len(context),
            )
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

    # Small/medium → render inline in a modal
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
    """Build a modal view showing `context`. Splits into multiple section blocks
    because Slack's per-section limit is 3000 chars and modals allow ~100 blocks.
    """
    from app.slack.blocks import _split_text_for_blocks, _SEC_MAX

    header_title = _truncate_slack(title or "Details", 24)
    chunks = _split_text_for_blocks(context, _SEC_MAX - 10)
    blocks: list[dict] = []
    # Cap at ~90 sections so we stay under Slack's 100-block view limit.
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


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _resolve_user(
    slack_user_id: str, team_id: str, channel_id: str = "",
) -> str:
    if not slack_user_id or not team_id:
        return ""

    uid = await get_user_uid(slack_user_id, team_id)
    if uid:
        return uid

    try:
        from slack_sdk import WebClient
        from app.slack.config import get_workspace

        ws = await get_workspace(team_id)
        if not ws or not ws.get("bot_token"):
            return ""
        client = WebClient(token=ws["bot_token"])
        info = client.users_info(user=slack_user_id)
        profile = (info.get("user") or {}).get("profile", {})
        email = profile.get("email", "")
        display_name = profile.get("display_name") or profile.get("real_name", "")
        if email:
            uid = await auto_map_user_by_email(
                slack_user_id, team_id, email, display_name,
            )
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
