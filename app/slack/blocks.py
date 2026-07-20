"""DEPRECATED — superseded by app/channels/slack/blocks.py.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Card → Slack Block Kit renderer.

Converts our internal card_data dicts (approval, question, progress, completion,
failure) into Slack Block Kit JSON for rich interactive messages.

Button action_id conventions (parsed in app/slack/actions.py):
  card_approve_{task_id}
  card_changes_{task_id}
  card_reply_{task_id}_{index}    # index into card["options"]
  card_replan_{task_id}
  card_retry_{task_id}
  card_cancel_{task_id}
"""
import logging
from typing import Optional

_log = logging.getLogger(__name__)

# Slack button-text limit is 75 chars. Plain text limits.
_BTN_MAX = 75
_SEC_MAX = 2900


def render_card_as_blocks(card: dict) -> Optional[list[dict]]:
    """Convert a card_data dict to Slack Block Kit blocks.

    Returns a list of block dicts, or None if the card type is unknown
    (caller should fall back to plain text).
    """
    card_type = card.get("card_type", "")
    if card_type == "approval":
        return _render_approval(card)
    if card_type == "question":
        return _render_question(card)
    if card_type == "progress":
        return _render_progress(card)
    if card_type == "completion":
        return _render_completion(card)
    if card_type == "failure":
        return _render_failure(card)
    return None


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _split_text_for_blocks(text: str, chunk_max: int = _SEC_MAX) -> list[str]:
    """Split `text` into chunks ≤ chunk_max, preferring newline boundaries.

    Used for long fields (plans, diffs, transcripts) that exceed Slack's
    3000-char-per-section limit. Each chunk becomes its own section block.
    """
    if not text:
        return []
    if len(text) <= chunk_max:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > chunk_max:
        # Try to break on a newline in the last 400 chars of the window
        window = remaining[:chunk_max]
        split_at = window.rfind("\n", max(0, chunk_max - 400), chunk_max)
        if split_at <= 0:
            split_at = chunk_max
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _code_blocks_from_text(text: str, max_blocks: int = 20) -> list[dict]:
    """Render `text` as one or more code-fenced section blocks, splitting as
    needed to respect Slack's 3000-char section limit. Trailing chunks beyond
    `max_blocks` are replaced with a "… [truncated]" marker so we don't blow
    through Slack's 50-block message cap."""
    # Leave ~10 chars headroom for the ``` fences
    chunks = _split_text_for_blocks(text, _SEC_MAX - 10)
    blocks: list[dict] = []
    overflow = False
    for i, chunk in enumerate(chunks):
        if i >= max_blocks:
            overflow = True
            break
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{chunk}```"},
        })
    if overflow:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_Plan continues for {len(chunks) - max_blocks} more "
                        f"section(s); open in the control panel to read the rest._",
            }],
        })
    return blocks


def _markdown_blocks_from_text(text: str, max_blocks: int = 20) -> list[dict]:
    """Same as _code_blocks_from_text but without code-fence wrapping — use
    for prose fields like `body` where we want Slack markdown rendered."""
    chunks = _split_text_for_blocks(text, _SEC_MAX)
    blocks: list[dict] = []
    overflow = False
    for i, chunk in enumerate(chunks):
        if i >= max_blocks:
            overflow = True
            break
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": chunk},
        })
    if overflow:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_Continues for {len(chunks) - max_blocks} more "
                        f"section(s); open in the control panel to read the rest._",
            }],
        })
    return blocks


def _artifacts_section(artifacts: list[dict]) -> Optional[dict]:
    if not artifacts:
        return None
    lines = []
    for art in artifacts[:10]:
        art_type = art.get("type", "")
        if art_type == "branch":
            lines.append(f"🌿 `{art.get('ref', '')}`")
        elif art_type == "file":
            lines.append(f"📁 `{art.get('path', '')}`")
        elif art_type == "url":
            lines.append(f"🔗 <{art.get('url', '')}>")
        else:
            lines.append(f"• {art.get('ref') or art.get('path') or art.get('url') or '?'}")
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Artifacts:*\n" + "\n".join(lines)},
    }


def _render_approval(card: dict) -> list[dict]:
    task_id = card.get("task_id", "")
    title = card.get("title", "Plan ready")
    body = card.get("body", "")

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": _truncate(f"📋 {title}", 150), "emoji": True}},
    ]
    if body:
        blocks.extend(_markdown_blocks_from_text(body))

    # Subtask summary
    subtasks = card.get("subtask_summary") or []
    if subtasks:
        lines = []
        for st in subtasks[:10]:
            lines.append(
                f"• *{st.get('title', 'Untitled')}* "
                f"— {st.get('template_id', '')} "
                f"({st.get('priority', 'normal')})"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "*Subtasks:*\n" + _truncate("\n".join(lines), _SEC_MAX)},
        })

    art = _artifacts_section(card.get("artifacts") or [])
    if art:
        blocks.append(art)

    # Context (full plan) is not rendered inline — the View Details button
    # opens a modal showing the full content. For very large plans the
    # context is uploaded as a file snippet alongside the card (handled in
    # outbound.relay_card_to_slack).
    context = card.get("context", "")
    has_details = bool(context and context.strip())

    action_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Approve Plan", "emoji": True},
            "style": "primary",
            "action_id": f"card_approve_{task_id}",
            "value": "approve",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ Request Changes", "emoji": True},
            "action_id": f"card_changes_{task_id}",
            "value": "request_changes",
        },
    ]
    if has_details:
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "📄 View Plan", "emoji": True},
            "action_id": f"card_details_{task_id}_approval",
            "value": "view_details",
        })

    blocks.append({
        "type": "actions",
        "elements": action_elements,
    })
    return blocks




def _render_question(card: dict) -> list[dict]:
    task_id = card.get("task_id", "")
    title = card.get("title", "Question")
    body = card.get("body", "")
    context = card.get("context", "")
    options = card.get("options") or []

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": _truncate(f"❓ {title}", 150), "emoji": True}},
    ]
    if body:
        blocks.extend(_markdown_blocks_from_text(body))

    has_details = bool(context and context.strip())

    if options:
        elements = []
        for i, opt in enumerate(options[:5]):  # Slack caps at 5 buttons per actions block
            label = _truncate(opt.get("label", f"Option {i + 1}"), _BTN_MAX)
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": label, "emoji": True},
                "action_id": f"card_reply_{task_id}_{i}",
                "value": opt.get("value", opt.get("label", "")),
            })
        blocks.append({"type": "actions", "elements": elements})

    # Replan + optional View Details button
    secondary_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "🔄 Re-plan", "emoji": True},
            "style": "danger",
            "action_id": f"card_replan_{task_id}",
            "value": "replan",
        },
    ]
    if has_details:
        secondary_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "📄 View Details", "emoji": True},
            "action_id": f"card_details_{task_id}_question",
            "value": "view_details",
        })
    blocks.append({"type": "actions", "elements": secondary_elements})

    # Instructions for custom answer — Slack doesn't have inline text inputs in
    # messages (only modals), so guide the user to reply with @mention format.
    if card.get("allow_custom", True):
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_Prefer a custom answer? Reply in this channel with your response and the assistant will pick it up._",
            }],
        })

    return blocks


def _render_progress(card: dict) -> list[dict]:
    title = card.get("title", "Progress")
    body = card.get("body", "")
    return [{
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"🔄 *{title}*\n{_truncate(body, _SEC_MAX)}"},
    }]


def _render_completion(card: dict) -> list[dict]:
    task_id = card.get("task_id", "")
    title = card.get("title", "Completed")
    body = card.get("body", "")
    context = card.get("context", "")
    blocks: list[dict] = [{
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"✅ *{title}*\n{_truncate(body, _SEC_MAX)}"},
    }]
    art = _artifacts_section(card.get("artifacts") or [])
    if art:
        blocks.append(art)
    if context and context.strip():
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "📄 View Details", "emoji": True},
                "action_id": f"card_details_{task_id}_completion",
                "value": "view_details",
            }],
        })
    return blocks


def _render_failure(card: dict) -> list[dict]:
    task_id = card.get("task_id", "")
    title = card.get("title", "Failed")
    body = card.get("body", "")
    context = card.get("context", "")
    options = card.get("options") or []

    blocks: list[dict] = [{
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"❌ *{title}*\n{_truncate(body, _SEC_MAX)}"},
    }]

    has_details = bool(context and context.strip())

    elements = []
    if options:
        for opt in options[:5]:
            opt_id = opt.get("id", "")
            label = _truncate(opt.get("label", opt_id.title()), _BTN_MAX)
            # Map known option ids to consistent action_ids
            if opt_id == "retry":
                action_id = f"card_retry_{task_id}"
            elif opt_id == "cancel":
                action_id = f"card_cancel_{task_id}"
            else:
                action_id = f"card_reply_{task_id}_{opt_id}"
            btn = {
                "type": "button",
                "text": {"type": "plain_text", "text": label, "emoji": True},
                "action_id": action_id,
                "value": opt.get("value", opt_id),
            }
            style = opt.get("style")
            if style in ("primary", "danger"):
                btn["style"] = style
            elements.append(btn)

    if has_details:
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "📄 View Details", "emoji": True},
            "action_id": f"card_details_{task_id}_failure",
            "value": "view_details",
        })

    if elements:
        blocks.append({"type": "actions", "elements": elements})

    return blocks


def strip_response_buttons(blocks: list[dict]) -> list[dict]:
    """Return a copy of `blocks` with response-action buttons removed.

    A "response-action button" is any button whose action_id starts with
    card_approve_ / card_changes_ / card_replan_ / card_retry_ /
    card_cancel_ / card_reply_ — i.e. the buttons that drive
    handle_card_response. The card_details_ button is KEPT so the user
    can still view the plan / attachments after responding.

    Action blocks that become empty after filtering are dropped.
    """
    RESPONSE_PREFIXES = (
        "card_approve_", "card_changes_", "card_replan_",
        "card_retry_", "card_cancel_", "card_reply_",
    )
    out: list[dict] = []
    for b in blocks or []:
        if b.get("type") == "actions":
            kept = [
                e for e in (b.get("elements") or [])
                if not any((e.get("action_id", "") or "").startswith(p) for p in RESPONSE_PREFIXES)
            ]
            if kept:
                out.append({"type": "actions", "elements": kept})
            # else: drop the actions block entirely
        else:
            out.append(b)
    return out


def responded_footer_block(responder: str, action: str, value: str = "") -> dict:
    """Build a context block summarising who responded and what they did."""
    text = f"✓ Responded by *{responder}* — {action}"
    if value:
        text += f"\n> {_truncate(value, 500)}"
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }
