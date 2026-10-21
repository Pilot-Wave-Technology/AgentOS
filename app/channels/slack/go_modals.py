"""A decision that needs a sentence asks for it in a box, not a nudge.

WHAT THIS REPLACES. Pressing *revise* or *decline* on a plan returned
a message saying "reply in this channel with what should change, and
the assistant will pick it up". Everything about that is worse than it
looks:

  * THE DECISION IS NOT RECORDED. `handle_park` returns before calling
    `decide`, so nothing happened at all -- the card still waits, and
    the person who pressed the button reasonably believes they have
    answered it.
  * THE REASON BECOMES A LOOSE MESSAGE. Typed into the channel, it is
    a chat line that something else has to associate with a question.
  * AND THE PROMPT IS EPHEMERAL. Only the presser sees it, in Slack,
    never persisted, so a second person finds a card nobody appears to
    have touched.

A modal fixes all three at once: the reason arrives WITH the decision,
in one submission, and `decide(…, text=…)` and `record_decision` -- both
of which already take it -- do the rest. Nothing downstream changes.

WHICH VERDICTS NEED ONE is a property of the verdict, not of the
button that produced it, so it lives here beside the modal rather than
being re-decided at each call site. Approve deliberately does NOT: a
one-click approve is the point of putting the card in Slack, and a
dialog on the common case is friction for nothing.

THE MODAL CANNOT BE THE ONLY DOOR. `views.open` needs a `trigger_id`,
which expires in three seconds and is absent if Slack retries the
interaction -- so every caller keeps its text fallback for when the
open fails. A decision surface that works only when the network is
quick is not a decision surface.
"""

from __future__ import annotations

import json
import logging

_log = logging.getLogger(__name__)

CALLBACK_ID = "go_reason_modal"

# Verdicts that mean "this is not what I wanted" and are useless
# without saying why. A revise with no reason restarts the same plan.
_NEEDS_REASON = {
    "park": {"revise", "decline", "declined"},
    "errand": {"decline", "declined"},
}

_TITLES = {
    ("park", "revise"): ("Request changes", "What should change?"),
    ("park", "decline"): ("Decline the plan", "Why is this not right?"),
    ("park", "declined"): ("Decline the plan", "Why is this not right?"),
    ("errand", "decline"): ("Decline the errand", "Why not?"),
    ("errand", "declined"): ("Decline the errand", "Why not?"),
}

_PLACEHOLDERS = {
    "park": "E.g. 'Use the existing auth helper rather than a new one.'",
    "errand": "E.g. 'Not now — we are mid-release.'",
}


def needs_reason(kind: str, value: str) -> bool:
    """Does this verdict require a sentence before it can be recorded?"""
    return (value or "").strip().lower() in _NEEDS_REASON.get(kind, set())


def build_view(kind: str, subject_id: str, value: str, *,
               channel_id: str = "", message_ts: str = "") -> dict:
    """The Block Kit view. Separate from opening it so it can be tested
    without a workspace, a token or a trigger."""
    verdict = (value or "").strip().lower()
    title, prompt = _TITLES.get(
        (kind, verdict), ("Add a reason", "Say what should happen"))
    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "private_metadata": json.dumps({
            "kind": kind, "subject_id": str(subject_id), "value": verdict,
            "channel_id": channel_id, "message_ts": message_ts,
        }),
        # Slack truncates a title over 24 characters without saying so.
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [{
            "type": "input",
            "block_id": "reason",
            "label": {"type": "plain_text", "text": prompt[:150]},
            "element": {
                "type": "plain_text_input",
                "action_id": "text",
                "multiline": True,
                "min_length": 3,
                "max_length": 2000,
                "placeholder": {
                    "type": "plain_text",
                    "text": _PLACEHOLDERS.get(kind, "Say what should happen"),
                },
            },
        }],
    }


async def open_modal(pool, *, team_id: str, trigger_id: str, kind: str,
                     subject_id: str, value: str, channel_id: str = "",
                     message_ts: str = "") -> bool:
    """Open it. False means the caller must fall back to text."""
    if not trigger_id:
        return False
    try:
        from app.channels.slack.client import make_async_client
        from app.channels.slack.config import get_workspace
    except ImportError:                                        # pragma: no cover
        return False

    ws = await get_workspace(team_id, pool)
    if not ws or not ws.get("bot_token"):
        return False

    try:
        client = make_async_client(ws["bot_token"])
        await client.views_open(
            trigger_id=trigger_id,
            view=build_view(kind, subject_id, value,
                            channel_id=channel_id, message_ts=message_ts))
        _log.info("Slack: opened %s reason modal for %s", kind, subject_id)
        return True
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack: views.open failed for %s %s: %s",
                     kind, subject_id, exc)
        return False


def read_reason(view: dict) -> str:
    """The typed text, from Slack's deeply nested state object."""
    return ((view.get("state") or {}).get("values", {})
            .get("reason", {}).get("text", {}).get("value", "") or "").strip()


def read_meta(view: dict) -> dict:
    try:
        return json.loads(view.get("private_metadata", "") or "{}")
    except json.JSONDecodeError:
        return {}


async def tell(pool, *, team_id: str, channel_id: str, slack_user_id: str,
               text: str) -> bool:
    """Say something to one person in the channel, after a modal closed.

    WHY THIS EXISTS AT ALL. A `view_submission` has no `response_url` --
    that is a property of a block action -- and a modal may answer only
    with a validation error or by closing. So a handler's prose refusal
    ("you don't have access", "that question no longer exists") has
    nowhere to go once the dialog is gone, and the person is left with
    a closed modal and no idea their decision did not land.
    """
    if not channel_id or not slack_user_id or not text:
        return False
    try:
        from app.channels.slack.client import make_async_client
        from app.channels.slack.config import get_workspace
        ws = await get_workspace(team_id, pool)
        if not ws or not ws.get("bot_token"):
            return False
        client = make_async_client(ws["bot_token"])
        await client.chat_postEphemeral(
            channel=channel_id, user=slack_user_id, text=text)
        return True
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack: could not tell %s in %s: %s",
                     slack_user_id, channel_id, exc)
        return False


# ── asking for more work, at the closeout ────────────────────────────

MORE_CALLBACK_ID = "go_more_work_modal"


def build_more_view(story_id: str, *, channel_id: str = "",
                    message_ts: str = "") -> dict:
    """The box for "what else needs doing".

    `more_work` already refuses an empty `what` -- "more work with
    nothing said is not a request" -- so the only question was where
    the sentence comes from. It used to be the tail of a slash command
    (`/run more <what>`), which the card never mentioned.
    """
    return {
        "type": "modal",
        "callback_id": MORE_CALLBACK_ID,
        "private_metadata": json.dumps({
            "story_id": str(story_id), "channel_id": channel_id,
            "message_ts": message_ts,
        }),
        "title": {"type": "plain_text", "text": "Ask for more work"},
        "submit": {"type": "plain_text", "text": "Start it"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": "This starts a *new plan* on the story, which is "
                        "why it asks rather than just doing it."}},
            {
                "type": "input",
                "block_id": "reason",
                "label": {"type": "plain_text", "text": "What else needs doing?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "text",
                    "multiline": True,
                    "min_length": 3,
                    "max_length": 2000,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "E.g. 'Add rate limiting to the /divide endpoint.'",
                    },
                },
            },
        ],
    }


async def open_more_modal(pool, *, team_id: str, trigger_id: str,
                          story_id: str, channel_id: str = "",
                          message_ts: str = "") -> bool:
    """Open it. False means the caller must fall back to text."""
    if not trigger_id:
        return False
    try:
        from app.channels.slack.client import make_async_client
        from app.channels.slack.config import get_workspace
    except ImportError:                                        # pragma: no cover
        return False

    ws = await get_workspace(team_id, pool)
    if not ws or not ws.get("bot_token"):
        return False
    try:
        client = make_async_client(ws["bot_token"])
        await client.views_open(
            trigger_id=trigger_id,
            view=build_more_view(story_id, channel_id=channel_id,
                                 message_ts=message_ts))
        _log.info("Slack: opened more-work modal for story %s", story_id)
        return True
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack: views.open failed for more-work %s: %s",
                     story_id, exc)
        return False
