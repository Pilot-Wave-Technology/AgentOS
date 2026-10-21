"""A card reaches Slack because it is a card, not because somebody remembered.

TWENTY-FOUR OF TWENTY-SEVEN CARD KINDS HAD NO SLACK PATH. Three call
sites in the whole codebase invoked a card relay -- assistant_relay,
diff_review and story_recovery -- and everything else wrote
`messages.card_data` and stopped. So `kernel_park` with its
approve/revise/decline, errand proposals, kernel verdicts and stalls,
PR cards, git node errors, plan review and the entire local-execution
family appeared in the panel and nowhere else. Not degraded to text:
absent.

That is a structural problem, not twenty-four missing calls. Adding
the calls would fix today and leave the twenty-fifth card kind to be
forgotten in exactly the same way.

WHERE THIS HOOKS, AND WHY NOT THE OBVIOUS PLACE. Every card poster
follows one idiom: INSERT the row, then call `notify_new_message` for
the durable cross-instance fan-out. The tempting hook is the LISTENER
side in `chat_module/fanout.py`, which already has the row in hand --
but that listener runs in EVERY web instance, so a Slack post from
there would be made once per instance. The publisher side runs once,
in the process that wrote the row, which is the property a side effect
needs.

Only card-bearing rows relay from here. Plain messages already go
through `relay_all_outbound` from the chat routers, and relaying both
here would double every one of them.
"""

from __future__ import annotations

import json
import logging

_log = logging.getLogger(__name__)


async def relay_card_for_message(pool, thread_id: str, message_id: str) -> bool:
    """If this message carries a card, mirror it to every platform.

    Returns True when something was relayed, for the tests. Never
    raises: it runs on the edge of whatever just posted a card, and
    Slack being unreachable must not fail that.
    """
    try:
        row = await pool.fetchrow(
            "SELECT thread_id, content, card_data FROM messages WHERE id = $1",
            message_id)
        if row is None or row["card_data"] is None:
            return False

        card = row["card_data"]
        if isinstance(card, str):
            card = json.loads(card) if card else None
        if not isinstance(card, dict) or not card:
            return False

        goal_id = await pool.fetchval(
            "SELECT goal_id FROM threads WHERE id = $1", thread_id)
        if not goal_id:
            return False

        from app.channels.slack.outbound import relay_card
        await relay_card(goal_id, thread_id, card, row["content"] or "",
                         msg_id=message_id)
        return True
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("card fan-out failed for message %s: %s", message_id, exc)
        return False
