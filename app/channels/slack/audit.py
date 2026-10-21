"""Leave a trace in the thread when somebody decides something from Slack.

A SLACK BUTTON'S REPLY IS EPHEMERAL: visible to the person who pressed
it, in Slack, and never persisted anywhere. The underlying action is
ledgered and the card shows as decided, so the STATE is right -- but
somebody reading the thread in the panel sees a card change with no
record of who pressed what, where, or when. The ledger has it, and the
ledger is not what a person reads when they are catching up on a
story.

So every Slack-originated decision writes one short line into the
thread. Not a card -- a card would re-trigger the fan-out and post
itself back to Slack -- just a message, with `platform` set so the
panel shows where it came from.

`role='agent'`, `sender='system'`: it is a notice about the
conversation rather than a turn in it, and giving it a human role
would make it look like something the person typed.
"""

from __future__ import annotations

import logging
import uuid

_log = logging.getLogger(__name__)


async def record_decision(pool, *, thread_id: str, user_uid: str,
                          action: str, detail: str = "") -> str | None:
    """Write "Approved by Ada, from Slack" into the thread. Returns msg id.

    Never raises. It runs after the decision has already been taken and
    committed, so failing here must not look like the decision failed.
    """
    if not thread_id or not action:
        return None
    try:
        who = await pool.fetchval(
            "SELECT display_name FROM users WHERE uid = $1", user_uid)
        who = (who or user_uid or "someone").strip()

        text = f"**{action}** by {who}, from Slack"
        if detail.strip():
            text += f" — {detail.strip()[:300]}"

        msg_id = str(uuid.uuid4())
        await pool.execute(
            """
            INSERT INTO messages (id, thread_id, sender, role, content,
                                  token_count, platform)
            VALUES ($1, $2, 'system', 'agent', $3, 0, 'slack')
            """, msg_id, thread_id, text)

        # Browser subscribers see it immediately. No card, so the card
        # fan-out inside notify_new_message does nothing -- which is what
        # stops this line being posted back into the Slack channel it
        # describes.
        from app.chat_module.repository import notify_new_message
        await notify_new_message(thread_id, msg_id)
        return msg_id
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Could not record the Slack decision in thread %s: %s",
                     thread_id, exc)
        return None


async def thread_for_story(pool, story_id: str) -> str:
    """The thread a story's decisions belong in, or ""."""
    if not story_id:
        return ""
    return await pool.fetchval(
        "SELECT active_thread_id FROM stories WHERE id = $1", story_id) or ""


async def thread_for_question(pool, question_id: int) -> str:
    """The thread behind a parked kernel question, or ""."""
    return await pool.fetchval(
        """
        SELECT COALESCE(e.driving_thread_id, s.active_thread_id, '')
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
          LEFT JOIN stories s ON s.id = e.story_id
         WHERE q.id = $1
        """, question_id) or ""
