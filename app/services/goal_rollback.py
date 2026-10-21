"""Undo a goal that was written seconds ago and could not be finished.

WHY THIS EXISTS RATHER THAN A TRANSACTION. Goal creation must leave
nothing behind when its Goal Orchestrator cannot be provisioned -- a
goal with no GO can plan nothing, and reporting it as created is the
worst answer for a caller with nobody watching a log.

The obvious shape is one transaction around the writes AND the
provisioning, rolled back on failure. It does not work, twice over:

  * `agent_contexts` has a foreign key to `goals`, so provisioning
    cannot run before the goal row exists. Trying it is how CATV9
    failed -- `Key (goal_id)=(CATV9) is not present in table "goals"`.

  * Putting provisioning INSIDE the creating transaction fixes the key
    and breaks something worse. `append_event` takes
    `pg_advisory_xact_lock(GLOBAL_CHAIN_LOCK_ID)`, held until commit,
    so the transaction would hold the platform-wide ledger lock across
    six seconds of gateway I/O. Every other append anywhere waits.

So the goal is written and committed, provisioning runs, and this
undoes the write when it fails.

WHAT IT DOES NOT UNDO, deliberately: the ledger. `goal.created` and
`goal.member_added` are already appended and the chain is immutable.
They stay as what they are -- the record of an attempt, not of a goal.
Nothing reads them to decide a goal exists; `goals` does that.

THE TABLE LIST IS EXPLICIT AND THAT IS A KNOWN COST. 21 of the 26
foreign keys into `goals` are `NO ACTION`, so there is no cascade to
lean on, and a future provisioning step that writes somewhere new will
not be undone until its table is added here. The final DELETE is what
catches that: if anything still references the goal, Postgres refuses,
and this says so loudly rather than leaving a half-removed goal.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Children first, parent last. Only tables that goal CREATION or GO
# PROVISIONING can write -- not the whole FK list, because a goal that
# never finished being created cannot have stories, tasks or git nodes.
_CHILDREN = (
    ("agent_contexts", "goal_id"),
    ("goal_agent_memberships", "goal_id"),
    ("agent_instances", "goal_id"),
    ("goal_resources", "goal_id"),
    ("threads", "goal_id"),
    ("goal_members", "goal_id"),
)


async def undo_goal_creation(pool, goal_id: str) -> bool:
    """Remove a just-created goal and everything creation made for it.

    True when the goal row is gone. False means something still
    references it and a person has to look -- the caller should say so
    rather than claim the goal was not created.
    """
    if not goal_id:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for table, col in _CHILDREN:
                    await conn.execute(
                        f"DELETE FROM {table} WHERE {col} = $1", goal_id)
                # The per-goal agent row, which is keyed by name rather
                # than by goal_id and so is not in the list above.
                await conn.execute(
                    "DELETE FROM agents WHERE id = $1",
                    f"go-{goal_id}".lower())
                await conn.execute("DELETE FROM goals WHERE id = $1", goal_id)
    except Exception:                                       # noqa: BLE001
        log.exception(
            "goal %s: could not be rolled back after provisioning failed; it "
            "is left in the database and needs a person", goal_id)
        return False
    log.info("goal %s: rolled back after provisioning failed", goal_id)
    return True
