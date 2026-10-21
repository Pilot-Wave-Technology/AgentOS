"""Every story state change, recorded — one function, one event.

WHAT WENT WRONG WITHOUT THIS. A story's state was written from eleven
places: two of them emitted `story.state_changed`, and nine did a bare
`UPDATE stories SET state = …`. Nothing downstream could tell the
difference between a transition that had not happened and one that had
happened silently.

The cost was not theoretical. The kernel marks a story done in
`go_wake_events`, with a bare UPDATE, and the memory_v2 bridge listens
for the ledger event — so a story could be accepted, delivered and
closed, and the assistant would never take its story-complete flush
turn. Both feature flags were ON. The machinery worked. It was simply
never told. The audit trail for KER12's first story reads:

    accepted -> in_progress          (emitted)
    done -> reviewed                 (emitted)

with the transition INTO done missing entirely — so the ledger says a
story became `reviewed` from a state it was never recorded as reaching.

WHY A HELPER AND NOT A TRIGGER. The ledger is hash-chained in the
application (`ledger/journal.append_event`), so a database trigger
cannot write to it without reimplementing the chain in PL/pgSQL. And a
transition has an ACTOR — the person who pressed the button, the
council, the engine — which SQL does not know. What can be enforced
mechanically is that nobody writes the column directly, and `ci/
check_story_state_ledger.sh` does exactly that.

IDEMPOTENT AND HONEST. The UPDATE is guarded so a no-op transition
writes no event: a story already `done` being marked done again is not
a transition and should not appear in the audit trail as one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def record_state(conn, story_id: str, goal_id: str | None,
                       new_state: str, *, actor_uid: str = "",
                       action: str = "", extra_set: str = "",
                       only_from: tuple = ()) -> str:
    """Move a story to `new_state` and record it. Returns the old state.

    Returns "" when nothing moved — the story was already there, does not
    exist, or `only_from` excluded it. Callers that care should check.

    `conn` must be a connection inside a transaction, so the UPDATE and
    the event commit together: an audit trail that can disagree with the
    row it describes is worse than none, because it is trusted.

    `goal_id` may be None, and is then read from the story. The engine's
    cancel paths select stories by merge group or node and genuinely do
    not know the goal — requiring it there would mean every caller
    joining a table to satisfy a signature.

    `extra_set` is raw SQL appended to the SET clause (`completed_at =
    now()` and friends). It is NEVER built from user input at any call
    site — it is a literal in the caller — and callers must keep it that
    way.
    """
    if goal_id is None:
        row = await conn.fetchrow(
            "SELECT state, title, goal_id FROM stories WHERE id = $1",
            story_id)
        goal_id = row["goal_id"] if row else None
    else:
        row = await conn.fetchrow(
            "SELECT state, title FROM stories WHERE id = $1 AND goal_id = $2",
            story_id, goal_id)
    if row is None:
        logger.warning("story_state: no story %s in goal %s", story_id, goal_id)
        return ""
    old_state = row["state"]
    if old_state == new_state:
        return ""
    if only_from and old_state not in only_from:
        logger.info("story_state: %s is %s, not one of %s — not moved to %s",
                    story_id, old_state, only_from, new_state)
        return ""

    sets = ["state = $3", "updated_at = now()"]
    if extra_set:
        sets.append(extra_set)
    moved = await conn.fetchval(
        f"UPDATE stories SET {', '.join(sets)} "
        f"WHERE id = $1 AND goal_id = $2 AND state = $4 RETURNING id",
        story_id, goal_id, new_state, old_state)
    if moved is None:
        # Somebody else moved it between the read and the write. Not an
        # error, and emphatically not an event: whatever they did, they
        # recorded it.
        return ""

    from app.ledger.actors import human_actor
    from app.ledger.journal import append_event
    from app.ledger.schema import STORY_STATE_CHANGED

    actor_id, actor_type = await human_actor(actor_uid)
    await append_event(
        conn,
        actor_id=actor_id, actor_type=actor_type,
        event_type=STORY_STATE_CHANGED,
        payload={
            "story_id": story_id,
            "title": row["title"],
            "from_state": old_state,
            "to_state": new_state,
            "action": action or new_state,
            "actor_uid": actor_uid,
        },
        context_goal_id=goal_id,
    )
    logger.info("story %s: %s -> %s (%s)", story_id, old_state, new_state,
                action or actor_uid or "system")

    # THE STORY IS OVER, SO ITS CHANNELS ARE. One channel per thread
    # means a goal otherwise collects one dead conversation per story
    # for ever, and the live ones get lost among them.
    #
    # Fire-and-forget, and deliberately so: this is the chokepoint for
    # a hash-chained ledger write, and a Slack workspace being
    # unreachable must not fail a state transition that has already
    # been recorded. `slack_retire.sweep` is the backstop for whatever
    # this loses.
    from app.services.slack_retire import TERMINAL
    if new_state in TERMINAL:
        try:
            import asyncio

            from app.database import get_pool
            from app.services.slack_retire import retire_story
            asyncio.create_task(retire_story(get_pool(), story_id))
        except Exception as exc:                               # noqa: BLE001
            logger.warning("story %s: could not retire Slack channels: %s",
                           story_id, exc)
    return old_state


async def record_states_where(conn, new_state: str, *, where: str,
                              params: tuple = (), actor_uid: str = "",
                              action: str = "") -> int:
    """Move every story matching `where`, recording each one. Returns how many.

    THE ENGINE CANCELS IN SETS, not one story at a time: a merge group is
    orphaned, a grind supersedes every in-flight diff story for a node.
    Those are real transitions and belong in the ledger like any other,
    but a single-story helper cannot express them — so this resolves the
    predicate to ids first and then records each, which also means each
    row gets its own accurate `from_state` rather than one event
    pretending a batch moved as a unit.

    `where` is a SQL fragment written as a literal at the call site and
    never assembled from user input; `params` are bound.
    """
    rows = await conn.fetch(f"SELECT id FROM stories WHERE {where}", *params)
    moved = 0
    for r in rows:
        if await record_state(conn, r["id"], None, new_state,
                              actor_uid=actor_uid, action=action):
            moved += 1
    if moved:
        logger.info("story_state: %d story(s) -> %s (%s)", moved, new_state,
                    action or "system")
    return moved


async def record_state_pooled(pool, story_id: str, goal_id: str | None,
                              new_state: str, **kw) -> str:
    """`record_state` for callers holding a pool rather than a connection.

    The transaction is opened here so the guarantee is the same either
    way — there is no version of this that writes the row without the
    event.
    """
    async with pool.acquire() as conn, conn.transaction():
        return await record_state(conn, story_id, goal_id, new_state, **kw)
