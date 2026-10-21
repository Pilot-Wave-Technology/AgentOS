"""The card that carries an answer back to the person who asked.

The fifth thing the kernel needs a person to see, after questions,
verdicts, pull requests and stalls — and shaped like the other four: a
kernel row with a `card_posted_at`, claimed before posting, swept into
the story thread from here. The kernel does not own `messages` and does
not learn to.

WHY AN ANSWER GETS A CARD AT ALL, rather than being something the
assistant reads out. Because the assistant may not be in the
conversation when it lands: answering takes a minute or two and the
person may have moved on. An answer that only exists as a row somebody
has to go and look for is an answer they will not read.

WHY EVERY ANSWER SAYS WHAT IT DID TO THE WORK. This is the failure the
whole channel was built around. Somebody asks "shouldn't this be
FastAPI?", gets a reasonable answer, and walks away believing they have
redirected the work — while the executor carries on building Flask,
because a question is not an instruction. The divergence between what
they think they did and what happened is worse than either outcome on
its own, so "nothing changed" is stated out loud, every time.
"""

from __future__ import annotations

import json
import logging
import uuid

from app.database import get_pool
from app.services.story_card_thread import newest_human_thread

log = logging.getLogger(__name__)

ANSWER_THREAD_SQL = """
    SELECT a.id, a.execution_id, a.question, a.answer, a.looked_at,
           a.held, a.hold_declined, a.failed, a.asked_by,
           e.goal_id, e.story_id, e.state,
           COALESCE(
             """ + newest_human_thread("e.story_id") + """,
             s.active_thread_id, s.thread_id) AS thread_id
      FROM kernel.answers a
      JOIN kernel.executions e ON e.id = a.execution_id
      LEFT JOIN public.stories s ON s.id = e.story_id
     WHERE a.card_posted_at IS NULL
     ORDER BY a.id
     LIMIT 20
"""


def _consequence(row) -> str:
    """What this answer did to the work. Said ONCE, and never left unsaid.

    THREE CASES, AND THEY WERE COLLAPSING INTO TWO. Watched live: the
    answerer decided to hold, the run reached VERIFYING before the
    answer was written, so the hold was dropped — and the card then said
    the story was held (the model's own words), that there was no work to
    stop (the fold-in), and that nothing had changed (this function).
    Three statements about one fact, disagreeing, in one card.

    So the model no longer narrates what happened to the work — it
    cannot know, the state is re-read after it returns — and this is the
    only place that says.
    """
    if row["held"]:
        # The `direction` card carries the fork and the buttons; this
        # only has to join the two so the person is not left wondering
        # whether the pause and their question are the same event.
        return ("**The work has stopped.** Answering this raised something "
                "you need to decide — there is a card just below asking "
                "for it, and the story picks up again the moment you "
                "answer.")
    if row["hold_declined"]:
        # A REAL DECISION WITH NO BUTTONS. The work did not stop because
        # there was nothing left to stop — so this must not read as
        # "nothing to see here", and must not pretend a pause happened.
        return ("**There is a decision above, and the work did not stop "
                "for it** — it had already moved past the point where it "
                "could be paused. Nothing has changed on its own. Act on "
                "it at the story's next card, or say what you want and it "
                "becomes a steer or a new plan.")
    if row["failed"]:
        return "_Nothing was changed or paused._"
    return ("_Nothing changed — asking does not alter the work. If you want "
            "something done differently, say so and it becomes a steer or "
            "a new plan._")


def build_card(row) -> dict:
    body = [row["answer"].strip(), "", _consequence(row)]
    if row["looked_at"]:
        body += ["", "_Looked at: " + row["looked_at"].strip() + "_"]
    return {
        "type": "agent_card",
        "card_type": "kernel_answer",
        "answer_id": row["id"],
        "execution_id": row["execution_id"],
        "goal_id": row["goal_id"],
        "story_id": row["story_id"],
        "agent": "executor",
        "agent_name": "Executor",
        "title": ("Answer — the work has stopped" if row["held"]
                  else "Answer"),
        "question": row["question"],
        "body": "\n".join(body),
        # NO BUTTONS, and that is the design: an answer asks nothing of
        # anybody. When it does need something, the `direction` card is
        # what carries the decision, with the buttons on it.
        "needs_human": False,
        "options": [],
        "allow_custom": False,
    }


async def _post(conn, row) -> bool:
    if not row["thread_id"]:
        log.warning("kernel answer card: execution %s (story %s) has no "
                    "thread; not posting", row["execution_id"],
                    row["story_id"])
        return False
    card = build_card(row)
    header = f"**Q: {row['question'].strip()}**\n\n" if row["question"] else ""
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, "
        "card_data, created_at) "
        "VALUES ($1,$2,'agent','executor',$3,$4::jsonb, NOW())",
        msg_id, row["thread_id"], header + card["body"], json.dumps(card))
    try:
        from app.chat_module.repository import notify_new_message
        await notify_new_message(row["thread_id"], msg_id)
    except Exception:                                         # noqa: BLE001
        # The row is in the thread; a refresh shows it. A failed push is
        # not a failed delivery.
        log.exception("kernel answer card: fan-out notify failed (story=%s)",
                      row["story_id"])

    # NO WAKE FROM HERE, and putting one here was a mistake worth
    # recording. This poster runs in the scheduler's card loop, and the
    # scheduler has no `CHAT_QUEUE_ENABLED` -- so `enqueue_system_wake`
    # returns None, `_deliver` releases its claim, and the wake is gone:
    #
    #   17:24:10 go_wakes: chat queue disabled in this process;
    #            errand_answer a:26 for goal ERR09 was not delivered
    #
    # A card poster writes a row to `messages`; a wake producer enqueues
    # a TURN. Those need different things from the process they run in,
    # which is exactly why the errand watch was moved out of this loop
    # earlier the same day. The answer wake is produced by
    # `errand_answer_wake.sweep`, on the control panel, beside the other
    # wake producers.
    return True


async def sweep() -> int:
    """Post every unposted answer. Claim before posting, like the rest."""
    pool = get_pool()
    posted = 0
    async with pool.acquire() as conn:
        for row in await conn.fetch(ANSWER_THREAD_SQL):
            claimed = await conn.fetchval(
                "UPDATE kernel.answers SET card_posted_at = now() "
                "WHERE id = $1 AND card_posted_at IS NULL RETURNING id",
                row["id"])
            if claimed is None:
                continue
            try:
                if await _post(conn, row):
                    posted += 1
            except Exception:                                 # noqa: BLE001
                log.exception("kernel answer card: %s failed", row["id"])
    return posted
