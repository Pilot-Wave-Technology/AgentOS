"""Card → Slack Block Kit renderer.

Converts internal card_data dicts (approval, question, progress, completion,
failure) into Slack Block Kit JSON for rich interactive messages.

Button action_id conventions (parsed in app/slack/actions.py and channels/slack/inbound.py):
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
    # diff_review cards use `kind` instead of `card_type`
    card_type = card.get("card_type", "") or card.get("kind", "")
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
    if card_type == "diff_review":
        return _render_diff_review(card)
    if card_type == "dag_recovery":
        return _render_dag_recovery(card)
    return None


def _render_dag_recovery(card: dict) -> list[dict]:
    """DAG failure recovery card (Phase 3) — Replan / Retry / Abandon buttons.
    action_id `recover_{kind}_{story_id}` is handled in slack/actions.py."""
    story_id = card.get("story_id", "")
    blocked = card.get("mode") == "blocked"
    reason = card.get("reason", "") or ("The phase is blocked." if blocked else "The DAG failed.")
    options = card.get("options") or ["replan", "retry", "abandon"]
    decided = card.get("decided", "")

    header = ("❓ Phase blocked — reply to resume, or:" if blocked else "⚠️ Phase failed")
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(reason, _SEC_MAX)}},
    ]
    if decided:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*Recovery:* {decided}"}],
        })
        return blocks

    _LABELS = {"replan": "♻ Replan", "retry": "↻ Retry",
               "review_keep": "📥 Review & keep", "abandon": "✕ Abandon"}
    _STYLE = {"replan": "primary", "abandon": "danger"}
    elements: list[dict] = []
    for opt in options:
        if opt not in _LABELS or not story_id:
            continue
        btn = {
            "type": "button",
            "text": {"type": "plain_text", "text": _LABELS[opt], "emoji": True},
            "action_id": f"recover_{opt}_{story_id}",
            "value": story_id,
        }
        if opt in _STYLE:
            btn["style"] = _STYLE[opt]
        elements.append(btn)
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    return blocks


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _split_text_for_blocks(text: str, chunk_max: int = _SEC_MAX) -> list[str]:
    """Split `text` into chunks ≤ chunk_max, preferring newline boundaries."""
    if not text:
        return []
    if len(text) <= chunk_max:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > chunk_max:
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
    """Render `text` as one or more code-fenced section blocks."""
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
    """Render `text` as one or more section blocks (plain Slack markdown)."""
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

    blocks.append({"type": "actions", "elements": action_elements})
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
        for i, opt in enumerate(options[:5]):
            label = _truncate(opt.get("label", f"Option {i + 1}"), _BTN_MAX)
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": label, "emoji": True},
                "action_id": f"card_reply_{task_id}_{i}",
                "value": opt.get("value", opt.get("label", "")),
            })
        blocks.append({"type": "actions", "elements": elements})

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

    if card.get("allow_custom", True):
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "_Prefer a custom answer? Reply in this channel with your response and the assistant will pick it up._",
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


def _render_diff_review(card: dict) -> list[dict]:
    approval_id = card.get("approval_id", "")
    stat = card.get("stat", "") or "Changes ready for review"
    truncated = card.get("truncated", False)
    decided = card.get("decided", "")

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📊 Diff ready for review", "emoji": True}},
    ]
    if stat:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate(stat, _SEC_MAX)},
        })
    if truncated:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": "_Diff was truncated — open the file viewer for the full patch._"}],
        })

    if decided:
        label = "✓ Approved & pushed" if decided == "approve" else "✗ Rejected — sent back for revision"
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*Decision recorded:* {label}"}],
        })
        return blocks

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Approve & Push", "emoji": True},
                "style": "primary",
                "action_id": f"diff_approve_{approval_id}",
                "value": approval_id,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Approve & Push?"},
                    "text": {"type": "mrkdwn",
                             "text": "This will commit and push the implementation. Continue?"},
                    "confirm": {"type": "plain_text", "text": "Yes, approve & push"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✗ Reject", "emoji": True},
                "style": "danger",
                "action_id": f"diff_reject_{approval_id}",
                "value": approval_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📄 View Diff", "emoji": True},
                "action_id": f"diff_details_{approval_id}",
                "value": approval_id,
            },
        ],
    })
    return blocks


def plan_ready_blocks(story_id: str, story_title: str, plan_path: str = "") -> list[dict]:
    """Block Kit card posted to Slack when SHCA writes a plan in operator mode.

    Uses ctrl_approve_plan_{story_id} and ctrl_replan_{story_id} action IDs so
    the existing _handle_story_control dispatcher in actions.py handles the clicks.
    """
    path_hint = f"`{plan_path}`" if plan_path else "the workspace"
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": _truncate(f"📋 Plan ready — {story_title}", 150),
                     "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Assistant has written a plan to {path_hint}. "
                    "Review it in the control panel, then approve or request changes."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve Plan", "emoji": True},
                    "style": "primary",
                    "action_id": f"ctrl_approve_plan_{story_id}",
                    "value": story_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "♻ Request Changes", "emoji": True},
                    "action_id": f"ctrl_replan_{story_id}",
                    "value": story_id,
                },
            ],
        },
    ]
    return blocks


def strip_response_buttons(blocks: list[dict]) -> list[dict]:
    """Return a copy of `blocks` with response-action buttons removed.

    card_details_ buttons are kept so users can still view plan/attachments
    after responding.
    """
    RESPONSE_PREFIXES = (
        "card_approve_", "card_changes_", "card_replan_",
        "card_retry_", "card_cancel_", "card_reply_",
        "diff_approve_", "diff_reject_",
        "ctrl_approve_plan_",
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