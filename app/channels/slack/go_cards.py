"""Block Kit for the cards the GO thread shows, and buttons that work.

TWENTY-FOUR OF TWENTY-SEVEN CARD KINDS HAD NO RENDERER, so
`render_card_as_blocks` returned None and the caller fell back to
plain text -- which drops exactly the buttons that make the card worth
having. Everything the GO thread shows was in that twenty-four:
kernel_park with its approve/revise/decline, errand proposals, kernel
verdicts, stalls and PR cards.

Two families, not twenty-four renderers:

  DECIDABLE cards carry an `options` list of {id, label, value, style}
  -- kernel_park and errand_proposal both do, and so does the existing
  `question` card. One renderer serves them, keyed by which decide
  endpoint the card belongs to.

  REPORTING cards are a title and a body. kernel_verdict, kernel_pr,
  kernel_stall and the local-execution family are all this. They had
  no renderer because nobody had written twenty of them; they need one
  between them.

THE ACTION IDS MATTER MORE THAN THEY LOOK. A button whose action_id
nothing can parse produces a dead click and one INFO line -- no error
to the user, no error to us. The ids emitted here are the ids
`actions.py` matches, and a test walks both to prove it.
"""

from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

# `park_<value>_<question_id>` and `errand_<value>_<proposal_id>`.
# The value can contain hyphens; the id is always the trailing integer.
PARK_ACTION_RE = re.compile(r"^park_([a-z_]+)_(\d+)$")
ERRAND_ACTION_RE = re.compile(r"^errand_([a-z_]+)_(\d+)$")
# `closeout_<value>_<story_id>`. The story id is short hex, NOT an
# integer like the other two, so it needs its own pattern -- reusing
# `(\d+)` here would silently never match and the button would be dead.
CLOSEOUT_ACTION_RE = re.compile(r"^closeout_([a-z_]+)_([0-9a-z]{4,})$")

# Which card kinds render as a decision, and what their buttons address.
DECIDABLE = {
    "kernel_park": ("park", "question_id"),
    "errand_proposal": ("errand", "proposal_id"),
    # EVERY LANE HAS MERGED AND THE CARD SAYS SO -- "mark it done, or
    # ask for more work and it starts a new plan". It said that as
    # prose with no buttons, so in Slack the only way to actually ask
    # was the `/run more <what>` slash command, which nothing on the
    # card mentions.
    "closeout_offer": ("closeout", "story_id"),
}

# Reporting cards: shown, not answered. Listed rather than defaulted so
# that a NEW decidable kind cannot quietly render as read-only and lose
# its buttons — the coverage test fails on anything in neither set.
REPORTING = {
    "kernel_verdict", "kernel_pr", "kernel_stall", "kernel_stall_cleared",
    "kernel_answer", "review", "plan_review", "plan_approved",
    "plan_rejected", "completion_claimed", "submit_failed", "pr_closed",
    "push_parked", "more_work_queued",
    "git_pr_opened", "git_pr_closed_unmerged", "git_node_error",
    "story_state_snapshot", "claude_tool_progress", "phase_task_progress",
}

_SECTION_LIMIT = 2900          # Slack's is 3000; leave room for markup
_BUTTON_LIMIT = 75


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _body_blocks(card: dict) -> list[dict]:
    title = _trim(card.get("title") or "", 150)
    body = _trim(card.get("body") or "", _SECTION_LIMIT)
    blocks: list[dict] = []
    if title:
        blocks.append({"type": "header",
                       "text": {"type": "plain_text", "text": title,
                                "emoji": True}})
    if body:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": body}})
    return blocks


def render(card: dict) -> list[dict] | None:
    """Blocks for a GO card, or None if this module does not own the kind."""
    # EITHER SPELLING. Most cards carry `card_type`; diff_review and
    # the local-execution family carry `kind`. `blocks.py` already
    # reconciles the two for its own dispatch and then hands us the raw
    # card, so reading only `card_type` here meant every `kind` card
    # fell straight through to None -- including `closeout_offer`,
    # whose text invites an action it then rendered no button for.
    kind = card.get("card_type") or card.get("kind") or ""
    if kind in DECIDABLE:
        return _render_decidable(card, kind)
    if kind in REPORTING:
        return _render_reporting(card)
    return None


def _render_reporting(card: dict) -> list[dict] | None:
    blocks = _body_blocks(card)
    if not blocks:
        return None
    link = card.get("url") or card.get("pr_url") or ""
    if link:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"<{link}|Open>"}]})
    return blocks


def _render_decidable(card: dict, kind: str) -> list[dict] | None:
    prefix, id_field = DECIDABLE[kind]
    subject = card.get(id_field)
    if subject in (None, ""):
        # Without the id the buttons address nothing, and a card with
        # dead buttons is worse than one with none.
        _log.warning("Slack: %s card has no %s — rendering read-only",
                     kind, id_field)
        return _render_reporting(card)

    blocks = _body_blocks(card)
    if card.get("decided"):
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f"_Already {card['decided']} — decided elsewhere._"}]})
        return blocks

    elements = []
    for opt in (card.get("options") or [])[:5]:      # Slack allows 5 per row
        value = str(opt.get("value") or opt.get("id") or "").strip()
        if not value:
            continue
        safe = re.sub(r"[^a-z_]", "_", value.lower())
        button = {
            "type": "button",
            "text": {"type": "plain_text",
                     "text": _trim(opt.get("label") or value, _BUTTON_LIMIT),
                     "emoji": True},
            "action_id": f"{prefix}_{safe}_{subject}",
            "value": value,
        }
        if opt.get("style") in ("primary", "danger"):
            button["style"] = opt["style"]
        # Anything destructive gets a confirm. Slack has no undo and a
        # mis-tap on a phone is one pixel from the button beside it.
        if opt.get("style") == "danger":
            button["confirm"] = {
                "title": {"type": "plain_text", "text": "Are you sure?"},
                "text": {"type": "mrkdwn",
                         "text": f"*{_trim(opt.get('label') or value, 100)}*"},
                "confirm": {"type": "plain_text", "text": "Yes"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            }
        elements.append(button)

    if not elements:
        return blocks or None
    blocks.append({"type": "actions", "elements": elements})

    if card.get("allow_custom"):
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": "_Reply in this channel to answer in your own words._"}]})
    return blocks
