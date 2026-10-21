"""H8e — cards and state changes are pushed, not merely readable.

`inbox` is a read, so "the plan approval appears in the assistant
thread" depended on a person happening to send a message first. That is
today's failure exactly: the live system injects a status snapshot
before every human turn and never otherwise, so an executor's question
sits in the database until somebody wanders past. A person waiting on an
approval they were never shown is indistinguishable, from their side,
from a story that is simply slow.

WHAT THIS IS AND IS NOT. It listens and it delivers; it does not decide.
Whether a card becomes a message, and what that message says, belongs to
the assistant with the thread in front of it. This hands over an
execution id and a kind, and the receiver reads the rows — the payload
is deliberately not the question text, because NOTIFY has an 8000-byte
ceiling a pasted stack trace would blow, and a listener that trusts a
payload keeps a second copy of the truth.

WHY NOT POLL. A poll fast enough to feel immediate is a query every
second per goal, and one slow enough to be cheap is a person watching a
spinner. The database already knows the moment a question is written.

DELIVERY IS INJECTED, and that is what makes this testable without a
gateway. E0 has no OpenClaw to post into; the gate passes a callback and
asserts on what it receives. In the local stack the callback posts to the
gateway's thread endpoint.
"""

from __future__ import annotations

from kernel.mailbox.schema import needs_a_person

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

CARD_CHANNEL = "assistant_card"
STATE_CHANNEL = "assistant_state"

# Two questions arriving together are one interruption (H5's window), so
# a listener that delivered each the instant it landed would undo the
# batching immediately downstream. This is deliberately shorter than the
# 90-second window: it coalesces the burst that arrives in one tick
# without holding a lone 3am question waiting for company.
COALESCE_S = 2.0


from kernel.assistant.cards import as_text, plan_of, story_key


async def _card(conn, question_id: int) -> dict | None:
    """The card, read from the row rather than trusted from the payload."""
    row = await conn.fetchrow(
        """
        SELECT q.id, q.execution_id, q.kind, q.question_text, q.blocking_on,
               q.reversible, q.status, q.payload, e.goal_id, e.story_id,
               COALESCE(q.batch_id, 'q-' || q.id::text) AS card,
               -- The same question `inbox` asks, for the same reason: a
               -- plan on an AUTONOMOUS errand is the GO's to settle, so
               -- a card that says "needs a person" would send it to
               -- find one that is not coming.
               (COALESCE(s.work_kind, 'story') = 'errand'
                AND EXISTS (SELECT 1 FROM kernel.errand_schedules g
                             WHERE g.goal_id = e.goal_id
                               AND g.approved_by IS NOT NULL
                               AND g.revoked_at IS NULL
                               AND (g.expires_at IS NULL
                                    OR g.expires_at > now())
                               AND (g.story_id IS NULL
                                    OR g.story_id = e.story_id))
               ) AS go_driven
          FROM questions q JOIN executions e ON e.id = q.execution_id
          LEFT JOIN stories s ON s.id = e.story_id
         WHERE q.id = $1
        """,
        question_id)
    if row is None or row["status"] != "OPEN":
        # Answered between the notify and the read. Not an error: the
        # person got there first, and delivering it now would show them a
        # card they have already dealt with.
        return None
    plan = (plan_of(row["payload"]) if row["kind"] == "authorization"
            else None)
    return {
        "event": "card", "goal": row["goal_id"],
        "story": story_key(row["story_id"], row["execution_id"]),
        "run": row["execution_id"],
        **({"plan": plan, "plan_text": as_text(plan)} if plan else {}),
        "card": row["card"], "question_id": row["id"], "kind": row["kind"],
        "asks": row["question_text"], "blocking": row["blocking_on"],
        "reversible": row["reversible"],
        # The field the assistant acts on: it may relay a revise or a
        # decline on a plan, never an approval. An AUTONOMOUS errand's
        # plan is the exception and belongs to the GO.
        "needs_human": needs_a_person(row["kind"]),
        "anchor": f"q-{row['id']}",
    }


async def listen(conn, deliver, goal_id: str | None = None,
                 stop: asyncio.Event | None = None,
                 coalesce_s: float = COALESCE_S) -> None:
    """Listen for cards and state changes; hand each to `deliver`.

    `deliver` is an async callable taking one dict. It is called once per
    event, and an exception in it is logged and swallowed — a delivery
    that fails must not take the listener down with it, or one bad
    thread stops every other goal's notifications.
    """
    queue: asyncio.Queue = asyncio.Queue()

    def on_card(_c, _pid, _chan, payload):
        queue.put_nowait(("card", json.loads(payload)))

    def on_state(_c, _pid, _chan, payload):
        queue.put_nowait(("state", json.loads(payload)))

    await conn.add_listener(CARD_CHANNEL, on_card)
    await conn.add_listener(STATE_CHANNEL, on_state)
    logger.info("assistant notifier: listening%s",
                f" for goal {goal_id}" if goal_id else "")

    try:
        while stop is None or not stop.is_set():
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            batch = [(kind, payload)]
            # Drain whatever arrived alongside it. Three questions in one
            # tick are one interruption, not three.
            deadline = asyncio.get_event_loop().time() + coalesce_s
            while asyncio.get_event_loop().time() < deadline:
                try:
                    batch.append(await asyncio.wait_for(
                        queue.get(),
                        timeout=deadline - asyncio.get_event_loop().time()))
                except (asyncio.TimeoutError, ValueError):
                    break

            for kind, payload in batch:
                event = None
                if kind == "card":
                    event = await _card(conn, int(payload["question_id"]))
                else:
                    event = {"event": "state", **payload}
                if event is None:
                    continue
                if goal_id and event.get("goal") not in (None, goal_id):
                    continue
                try:
                    await deliver(event)
                except Exception:                           # noqa: BLE001
                    logger.exception("assistant notifier: delivery failed "
                                     "for %s", event.get("event"))
    finally:
        await conn.remove_listener(CARD_CHANNEL, on_card)
        await conn.remove_listener(STATE_CHANNEL, on_state)
