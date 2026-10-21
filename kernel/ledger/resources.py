"""Resource activity onto the ledger.

WHY THIS EXISTS. `resource_events` is a second event store: 63 rows, no hash
chain, no append-only trigger, no foreign key to `actors`, and an `actor`
column holding the same mixed vocabulary the ledger just stopped using --
`local-dev-admin` (a uid) beside `story:g6s1` (a prefixed provenance string).
Ten call sites write it and one UI feed reads it.

IT PARTIALLY DUPLICATES THE LEDGER, which is the whole difficulty. 34 of the
63 are already on the chain because `goal_resources.py` dual-writes: it
emits `resource.goal.registered` AND inserts here. The other 29 come from
paths that only write here -- `chat.py` and `resource_publish.py` -- so they
exist nowhere auditable. A naive drain would record those 34 a second time
and make the ledger claim twice as much resource activity as happened.

So the pass skips a row whose resource already carries the equivalent ledger
event, and `dedupe_key` makes a re-run harmless either way. The table itself
stays: the UI reads it, and deleting a store to tidy the model is a bigger
change than making the ledger complete.
"""
from __future__ import annotations

import json
import logging

from kernel.ledger.actors import (EXECUTOR, SYSTEM_ACTOR, human_actor,
                                  kernel_actor)
from kernel.ledger.emit import append

logger = logging.getLogger(__name__)

# The ledger's own vocabulary, not this table's. `created`/`updated`/
# `published` are internal words; the chain speaks dotted event types.
_EVENT = {
    "created": "resource.goal.registered",
    "updated": "resource.goal.updated",
    "published": "resource.goal.published",
    "deleted": "resource.goal.deleted",
    "scanned": "resource.goal.scanned",
}


async def drain_resource_events(conn, limit: int = 500) -> int:
    """Ledger rows written for resource activity that had none."""
    rows = await conn.fetch(
        """SELECT re.id, re.resource_id, re.goal_id, re.event_type,
                  re.actor, re.details, re.created_at
             FROM public.resource_events re
            WHERE NOT EXISTS (
                  -- Already audited by the dual-writing path. Matched on the
                  -- resource rather than on time, because the two writes are
                  -- in one transaction but not one instant.
                  SELECT 1 FROM public.ledger_events le
                   WHERE le.payload->>'resource_id' = re.resource_id::text
                     AND le.event_type IN ('resource.goal.registered',
                                           'GOAL_RESOURCE_REGISTERED',
                                           'resource.goal.updated',
                                           'GOAL_RESOURCE_UPDATED'))
         ORDER BY re.created_at DESC
            LIMIT $1""", limit)

    written = 0
    for r in rows:
        event_type = _EVENT.get((r["event_type"] or "").lower())
        if event_type is None:
            # A verb nobody mapped. Named rather than dropped, so a new one
            # shows up on the chain instead of vanishing until someone
            # notices the counts disagree.
            event_type = f"resource.goal.{(r['event_type'] or 'activity').lower()}"

        actor, principal = await _who(conn, r["actor"], r["goal_id"])
        if await append(
                conn, event_type=event_type,
                actor_id=actor[0], actor_type=actor[1],
                payload={
                    "resource_id": str(r["resource_id"]),
                    "goal_id": r["goal_id"],
                    "actor_uid": principal[1],
                    "source_event": r["event_type"],
                    "provenance": r["actor"],
                    "details": _details(r["details"]),
                },
                on_behalf_of=principal[0] if actor[1] != "human" else None,
                goal_id=r["goal_id"],
                idempotency_key=f"resource_event:{r['id']}"):
            written += 1
    if written:
        logger.info("ledger drain: %s resource event(s)", written)
    return written


async def _who(conn, stamp, goal_id):
    """((actor_id, actor_type), (principal_id, principal_uid)).

    `resource_events.actor` is a uid for a person and `story:<id>` for work a
    story did. The second is not a person, so it resolves to that story's
    executor with the story's owner as the principal -- the same shape every
    other machine act on the chain uses.
    """
    s = (stamp or "").strip()
    if s.startswith("story:"):
        row = await conn.fetchrow(
            """SELECT e.kind, e.domain, ha.id AS human_id, ha.uid
                 FROM executions e
                 LEFT JOIN threads t  ON t.story_id = e.story_id
                 LEFT JOIN actors ha  ON ha.uid = t.created_by AND ha.type = 'user'
                WHERE e.story_id = $1
             ORDER BY e.id DESC LIMIT 1""", s.split(":", 1)[1])
        if row:
            actor = await kernel_actor(conn, EXECUTOR, row["kind"], row["domain"])
            return actor, (row["human_id"], row["uid"])
        return (SYSTEM_ACTOR, "system"), (None, None)

    human = await human_actor(conn, s)
    if human:
        uid = await conn.fetchval("SELECT uid FROM actors WHERE id = $1", human)
        return (human, "human"), (human, uid)
    return (SYSTEM_ACTOR, "system"), (None, None)


def _details(raw):
    """The resource's own metadata, kept small.

    Never the whole blob: it can carry a scan summary, and a payload is
    hashed into the chain and cannot be trimmed afterwards.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    return {k: str(v)[:200] for k, v in list(raw.items())[:8]}
