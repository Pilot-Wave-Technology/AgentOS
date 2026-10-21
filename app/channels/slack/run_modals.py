"""Pause, resume and cancel a run from Slack, with the panel's own words.

WHY THESE MOVED OFF `/goal`. Slack had `/goal pause` and `/goal resume`,
which froze dispatch for the WHOLE GOAL -- every story in it -- while
the panel's Executor bar offered pause, resume and cancel for the ONE
run you were looking at. The blunt instrument was the one exposed in
chat and the precise one was not, so the useful action was unreachable
from the place people were standing.

THE TEXT IS OPTIONAL IN ALL THREE CASES, and each means something
different, which is why the prompts are not shared:

  PAUSE   "Why are you pausing?" -- recorded with the hold.
  CANCEL  "Why are you cancelling?" -- recorded on the run, which is
          finished either way. This is the one that cannot be undone.
  RESUME  "What should it do?" -- NOT a note. It is delivered as a
          STEER: the executor reads it before its next model call and
          it stays in front of the model for the rest of the run. If it
          changes what "done" means, the executor writes a new plan and
          parks for approval.

The wording is lifted from `static/js/run_controls.js` deliberately. A
pause from Slack and a pause from the panel are the same call on the
same service, and two descriptions of one action is how people end up
believing they are two actions.

AND THE BOX IS NEVER THE ONLY WAY. `trigger_id` is valid for three
seconds, single-use, and absent when Slack retries the command, so
`/run pause <why>` with the text inline has to keep working -- and
does.
"""

from __future__ import annotations

import json
import logging

_log = logging.getLogger(__name__)

CALLBACK_ID = "run_control_modal"

_SPEC = {
    "pause": {
        "title": "Pause this executor?",
        "body": "It stops at its next step boundary — usually seconds, "
                "occasionally as long as one tool call. The sandbox stays "
                "up and the journal is kept, so Resume carries on from "
                "where it stopped. Nothing is lost.",
        "label": "Why are you pausing? (optional)",
        "submit": "Pause",
        "placeholder": "E.g. 'Need to check the staging data first.'",
    },
    "resume": {
        "title": "Resume this executor",
        "body": "It continues from where it stopped — it will not redo the "
                "last leg. *Anything you write below is delivered as a "
                "STEER:* the executor reads it before its next model call "
                "and it stays in front of the model for the rest of the "
                "run. If it changes what \"done\" means, the executor "
                "writes a new plan and parks for your approval.",
        "label": "What should it do? (optional — blank just resumes)",
        "submit": "Resume",
        "placeholder": "E.g. 'Use the existing retry helper.'",
    },
    "cancel": {
        "title": "Cancel this run?",
        "body": "A running leg stops at its next step boundary; a parked "
                "one ends on the next sweep. Any open question is closed.\n\n"
                "*This cannot be undone.* The story stays and can be "
                "started again, but this run and its progress are over.",
        "label": "Why are you cancelling? (optional)",
        "submit": "Cancel the run",
        "placeholder": "E.g. 'Superseded by the other approach.'",
    },
}


def build_view(action: str, thread_id: str, goal_id: str, *,
               channel_id: str = "") -> dict:
    """The Block Kit view. Built apart from opening it so the wording
    can be tested without a workspace, a token or a trigger."""
    spec = _SPEC[action]
    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "private_metadata": json.dumps({
            "action": action, "thread_id": thread_id, "goal_id": goal_id,
            "channel_id": channel_id,
        }),
        # Slack silently truncates a title past 24 characters.
        "title": {"type": "plain_text", "text": spec["title"][:24]},
        "submit": {"type": "plain_text", "text": spec["submit"]},
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": spec["body"]}},
            {
                "type": "input",
                "block_id": "reason",
                # OPTIONAL, and Slack needs telling: an input block is
                # required by default, so without this the dialog would
                # refuse a blank resume -- which is the ordinary case.
                "optional": True,
                "label": {"type": "plain_text", "text": spec["label"][:150]},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "text",
                    "multiline": True,
                    "max_length": 2000,
                    "placeholder": {"type": "plain_text",
                                    "text": spec["placeholder"]},
                },
            },
        ],
    }


async def open_run_modal(pool, *, team_id: str, trigger_id: str, action: str,
                         thread_id: str, goal_id: str,
                         channel_id: str = "") -> bool:
    """Open it. False means the caller must act on what it already has."""
    if not trigger_id or action not in _SPEC or not thread_id:
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
            view=build_view(action, thread_id, goal_id,
                            channel_id=channel_id))
        _log.info("Slack: opened %s modal for thread %s",
                  action, (thread_id or "")[:12])
        return True
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack: views.open failed for %s %s: %s",
                     action, (thread_id or "")[:12], exc)
        return False
