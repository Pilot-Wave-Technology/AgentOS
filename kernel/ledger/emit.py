"""Appending a kernel event. Never raises into the work it describes.

THE RULE THIS FILE EXISTS TO KEEP. An audit trail must not be able to break
the thing it is auditing. Every failure here is logged and swallowed: a
verdict must land even if its ledger row cannot, because losing the verdict
to save the record of it is the wrong trade in every direction.

The one thing NOT swallowed is silence. Every failure logs at WARNING with
the event type, because an audit trail that quietly stops is indistinguish-
able from a system that has stopped doing anything -- and the grants that
make this work are exactly the kind that go missing in a new environment.

CONTEXT IS SET SO THE LEDGER LINES UP WITH THE COST ROLLUP.
`context_conversation_id` is the thread id, which is the same key
`mv_cost_by_thread` groups on, so "what did this conversation do" and "what
did it cost" are one join apart rather than two different notions of a
conversation.
"""
from __future__ import annotations

import json
import logging

from kernel.ledger.chain import (GENESIS, GLOBAL_CHAIN_LOCK_ID, chain_hash,
                                 envelope_digest, new_event_id, payload_json)

logger = logging.getLogger(__name__)


async def append(conn, *, event_type: str, actor_id, actor_type: str,
                 payload: dict, on_behalf_of=None, goal_id: str | None = None,
                 thread_id: str | None = None, instance: str | None = None,
                 idempotency_key: str | None = None) -> str | None:
    """One ledger event. Returns its id, or None if it was not written.

    None covers both "already recorded" and "could not record", and the
    caller treats them the same because there is nothing useful either can
    do differently.
    """
    try:
        # SAVEPOINT, for the same reason as in `actors.kernel_actor`: a
        # caught database error still leaves the surrounding transaction
        # aborted, so a swallowed ledger failure would take the verdict or
        # the leg down with it. A nested asyncpg transaction rolls back only
        # what happened inside it.
        async with conn.transaction():
            return await _append(
                conn, event_type=event_type, actor_id=actor_id,
                actor_type=actor_type, payload=payload,
                on_behalf_of=on_behalf_of, goal_id=goal_id,
                thread_id=thread_id, instance=instance,
                idempotency_key=idempotency_key)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("ledger event not written (%s): %s", event_type, exc)
        return None


async def _append(conn, *, event_type, actor_id, actor_type, payload,
                  on_behalf_of, goal_id, thread_id, instance, idempotency_key):
    # Serialised against every other appender, including the control panel's.
    # Same lock id, which is why it is imported rather than restated.
    await conn.execute("SELECT pg_advisory_xact_lock($1)", GLOBAL_CHAIN_LOCK_ID)

    row = await conn.fetchrow("SELECT last_hash FROM chain_head WHERE id = 1")
    prev_hash = row["last_hash"] if row else GENESIS

    event_id = new_event_id()
    integrity = chain_hash(prev_hash, payload)
    # The forward-looking cover over WHO acted. Kept in `extensions` so it
    # needs no column and no backfill; a verifier checks it where present.
    extensions = {"envelope_digest": envelope_digest(
        event_type=event_type, actor_id=actor_id, actor_type=actor_type,
        on_behalf_of=on_behalf_of, payload=payload)}

    tag = await conn.execute(
        """INSERT INTO ledger_events
               (event_id, event_type, envelope_version, event_version,
                actor_id, actor_type, on_behalf_of,
                context_goal_id, context_conversation_id, context_task_id,
                classification, integrity_hash, previous_hash,
                payload, extensions, idempotency_key)
           VALUES ($1,$2,1,1,$3,$4,$5,$6,$7,$8,'normal',$9,$10,
                   $11::jsonb,$12::jsonb,$13)
           ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
           DO NOTHING""",
        event_id, event_type, actor_id, actor_type, on_behalf_of,
        # '' not NULL. These columns are NOT NULL DEFAULT '' and every
        # existing row follows that convention. Passing None dropped the
        # event entirely — so a run whose thread had been deleted lost its
        # audit record over a missing bit of context, which is the wrong
        # trade: the act still happened and is still worth recording.
        goal_id or "", thread_id or "",
        # WHICH INSTANCE. `actor_id` names the ROLE — one actor covers every
        # story/coding executor — so with four legs in flight it cannot say
        # which one acted. The execution is the unit of parallelism, and
        # putting it HERE rather than only in the payload makes it an
        # indexed envelope column (`idx_ledger_task`) instead of a JSON
        # probe. Prefixed, because legacy task ids are bare digits too and
        # `exec:1749` can never be mistaken for one.
        instance or "", integrity, prev_hash,
        payload_json(payload), json.dumps(extensions), idempotency_key)

    # THE PREDICATE IS REQUIRED, not decoration. `uq_idempotency` is a
    # PARTIAL unique index (`WHERE idempotency_key IS NOT NULL`), and a bare
    # `ON CONFLICT (idempotency_key)` cannot infer a partial index -- it
    # fails with "no unique or exclusion constraint matching". Every event
    # was being silently dropped and logged as unwritten.
    if not tag.endswith(" 1"):
        # Already recorded. The head must NOT move — advancing it for an
        # event that was not inserted would leave the next event chained to
        # a hash no row carries, breaking verification from that point on.
        return None

    await conn.execute(
        """INSERT INTO chain_head (id, last_event_id, last_hash, event_count)
           VALUES (1, $1, $2, 1)
           ON CONFLICT (id) DO UPDATE SET
               last_event_id = $1, last_hash = $2,
               event_count = chain_head.event_count + 1, updated_at = now()""",
        event_id, integrity)
    return str(event_id)
