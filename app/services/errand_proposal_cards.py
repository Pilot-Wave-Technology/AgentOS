"""I5f — GO's errand proposal, as a card in the goal thread.

WHY THE GOAL THREAD and not somebody's assistant. There is no story yet,
so there is no story thread, and there is no owner — the design's rule is
"any human in the goal may approve". Posting into one person's assistant
would silently pick an owner and leave everyone else unaware there was a
question. The goal thread is the one place everybody in the goal reads.

A POLL, NOT A NOTIFY, for the same reason `kernel_park_cards` polls: this
process may restart, deploy, or simply not have been running when GO
proposed. `card_posted_at IS NULL` catches every proposal whenever it
starts; a missed NOTIFY is gone.

THE CARD MUST SAY WHAT IS BEING AGREED TO. Not "GO wants to run an
errand" — the person is agreeing to own every run of it: their thread
drives it, their name is on the ledger, their budget pays. Reading the
card has to be enough to know that, because the alternative is somebody
approving on the strength of the task title.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from app.database import get_pool

log = logging.getLogger(__name__)

INTERVAL_S = 5.0


def build_card(row) -> dict:
    """The card the browser renders. Shape follows app/cards.py."""
    return {
        "type": "agent_card",
        "card_type": "errand_proposal",
        "proposal_id": row["id"],
        "goal_id": row["goal_id"],
        "agent": "go",
        "agent_name": "Goal Orchestrator",
        "title": "An errand to run without you",
        "body": body_for(row),
        # ALWAYS. There is no agent-answerable version of this question:
        # `errand_schedules` INSERT is a human privilege, so a card that
        # said otherwise would be offering something the database refuses.
        "needs_human": True,
        "options": [
            {"id": "approve", "label": "✓ Approve — I'll own this run",
             "value": "approve", "style": "primary"},
            {"id": "decline", "label": "✕ Decline",
             "value": "decline", "style": "danger"},
        ],
        # A decline carries a reason so GO does not simply re-propose.
        "allow_custom": True,
    }


def body_for(row) -> str:
    """What the errand IS, then what approving it means.

    THE OLD BODY DESCRIBED A PRODUCT THAT DOES NOT EXIST. It talked
    about "later runs", a schedule, and reusing an approved plan across
    runs -- recurrence that has never once happened: every grant ever
    written is `@once`, `max_runs=1`, `run_count=1`. Somebody reading it
    was being asked to agree to something more open-ended than what they
    were actually getting, which is the worst direction for a consent
    dialogue to be wrong in.

    SO THE TASK LEADS NOW. The old version put the bounds first on the
    reasoning that the task is "the part they would have guessed" --
    true when the interesting fact was a recurring grant. With one run
    and one approval, the only thing worth reading is WHAT IT WILL DO,
    and the ownership is a short line under it.
    """
    # BACKTICKS, NOT BOLD. This lands inside a bold sentence and
    # nested `**` does not render -- it comes out as literal asterisks
    # in the middle of the one line that says what approving does.
    machine = (f" on a `{row['sandbox_size']}` machine"
               if row["sandbox_size"] else "")
    expires = (f" It lapses if not started by "
               f"**{row['expires_at']:%Y-%m-%d %H:%M} UTC**."
               if row["expires_at"] is not None else "")

    out = [
        f"**{row['proposed_by']} proposes an errand: "
        f"{(row['name'] or '').strip() or 'untitled'}**", "",
        "**What it will do**", "",
        "```", row["task"].strip(), "```", "",
        f"**Why:** {row['rationale']}", "",
        "---", "",
        f"**Approving starts it.** It runs once{machine}. You own it: it "
        f"runs in your thread, the ledger records you as the principal, "
        f"and the cost is yours.{expires}", "",
        "_It plans its work first and the orchestrator settles that plan "
        "for you. **It still asks you before it closes** — when the work "
        "is done you choose whether it is finished or needs more. What "
        "you are agreeing to is that it STARTS without you, not that it "
        "finishes without you._", "",
        "_Not right? Ask for changes in the thread and it stays proposed "
        "until you are happy with it._",
    ]
    return "\n".join(out)


async def _pending(conn) -> list:
    return await conn.fetch(
        """
        SELECT p.id, p.goal_id, p.proposed_by, p.task, p.name, p.rationale,
               p.cron, p.max_runs, p.expires_at, p.sandbox_size,
               g.go_thread_id
          FROM kernel.errand_proposals p
          JOIN goals g ON g.id = p.goal_id
         WHERE p.card_posted_at IS NULL
           AND p.state = 'PENDING'
         ORDER BY p.created_at
         LIMIT 20
        """)


async def _post(conn, row) -> str:
    if not row["go_thread_id"]:
        # No goal thread to post into. Marked announced anyway: the
        # poller would otherwise retry for ever, and the proposal is
        # perfectly decidable through the endpoint.
        log.warning("errand proposal %s: goal %s has no goal thread; "
                    "not posting", row["id"], row["goal_id"])
        return ""

    card = build_card(row)
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, "
        "card_data, created_at) "
        "VALUES ($1,$2,'agent','go',$3,$4::jsonb, NOW())",
        msg_id, row["go_thread_id"], card["body"], json.dumps(card))

    # THE NOTIFY HAPPENS AFTER THE COMMIT, in `sweep`, and this is the
    # second time the same trap has been walked into here.
    #
    # It used to be `notify_new_message(..., conn=conn)` right here.
    # That fixed the ORIGINAL bug correctly -- a pooled notify fires
    # immediately, while the insert above is still uncommitted, so the
    # listener looked the message up, found nothing, and returned at
    # its `if not row` guard without a sound ("the errand approval card
    # only appears after a refresh", ERR09).
    #
    # But `notify_new_message` later grew a second job: relaying the
    # card to Slack. That relay reads the message row back through
    # `get_pool()` -- a DIFFERENT connection -- which cannot see this
    # transaction either. So the card reached the panel and never
    # reached Slack, silently, for exactly the same reason and in
    # exactly the same place. Observed on CALY5: the errand proposal
    # was on the GO thread in the UI and absent from its channel.
    #
    # Notifying after the commit fixes both at once and needs no
    # special case: the row is visible to every connection, so the
    # pooled notify and the pooled relay are both correct.
    return msg_id


async def sweep() -> int:
    """One pass. Returns how many cards were posted."""
    pool = get_pool()
    posted = 0
    async with pool.acquire() as conn:
        for row in await _pending(conn):
            # THE TRY IS OUTSIDE THE TRANSACTION, deliberately. Catching
            # inside it would COMMIT whatever the failed post had already
            # written and then skip the stamp — so the card is half in the
            # thread and the poller retries it for ever. Outside, a
            # failure rolls the whole row back and the next tick tries
            # again cleanly.
            #
            # And one bad proposal must not stop the others: the sweep
            # runs on the card tick alongside every other poster.
            msg_id = ""
            try:
                async with conn.transaction():
                    msg_id = await _post(conn, row)
                    if msg_id:
                        posted += 1
                    # STAMPED IN THE SAME TRANSACTION as the message. Two
                    # transactions is how a card gets posted twice.
                    await conn.execute(
                        "UPDATE kernel.errand_proposals SET card_posted_at "
                        "= now() WHERE id = $1", row["id"])
            except Exception:                               # noqa: BLE001
                log.exception("errand proposal %s: could not post",
                              row["id"])
                continue

            # OUTSIDE THE TRANSACTION, AND THAT IS THE POINT. Both the
            # browser fan-out and the Slack relay read the message row
            # back on their own connections; neither can see it until
            # this commit. See the long note in `_post`.
            if msg_id:
                try:
                    from app.chat_module.repository import notify_new_message
                    await notify_new_message(row["go_thread_id"], msg_id)
                except Exception:                           # noqa: BLE001
                    # The row is committed and a refresh shows it. A
                    # failed push is not a failed delivery.
                    log.exception("errand proposal %s: fan-out notify "
                                  "failed", row["id"])
    return posted


async def run_forever() -> None:
    log.info("errand proposal cards: watching for GO's proposals")
    while True:
        try:
            n = await sweep()
            if n:
                log.info("errand proposal cards: posted %s", n)
        except Exception:                                   # noqa: BLE001
            log.exception("errand proposal cards: sweep failed")
        await asyncio.sleep(INTERVAL_S)
