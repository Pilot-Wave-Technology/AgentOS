"""Carrying a run forward to the current state machine.

`run_state_machine` is versioned so a deploy never rewrites an in-flight
two-day story: each execution records the version it started under, and
that is what its transitions are checked against.

That is right, and it strands runs. Twice in one session:

  a story PARKED under v2 could not be cancelled, because v2 has no
  cancel edge from PARKED_HOT — so a run waiting on a question nobody
  would answer had no legal ending and held its admission slot for ever

  a story DONE under v3 could not be re-scoped, because v3 has no
  DONE -> PENDING edge

Both times the operator wanted something the CURRENT machine allows and
the run's own machine does not. Both times the workaround was to edit
`machine_version` by hand, which is the shape of a thing that should be
an operation.

AN OPERATOR ADOPTING NEW RULES IS NOT A DEPLOY IMPOSING THEM. That is
the whole distinction versioning exists to protect, and it survives
here: nothing carries a run forward on its own, and a deploy still
changes nothing about a run already in flight. What this adds is a
person saying "apply today's rules to this run", deliberately, with the
old version recorded.

FORWARD ONLY. Lowering a run's version would take away transitions it
may already have used, leaving a path in `execution_transitions` that
the machine says is impossible — the console would flag every one of
them red, correctly, for ever.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class CarryRefused(Exception):
    """The run cannot be carried forward, and why."""


async def current_version(conn) -> int:
    """The newest REAL machine.

    `is_fixture` is not decoration. This asked for `max(version)` and got
    900 — a machine `test_reconcile.py` inserts, which picked a high
    number to stay out of the way and stayed out of the way of every
    ORDERED read and none of the max() ones. A run carried forward to a
    fixture would be checked against transitions written for a test.
    """
    return await conn.fetchval(
        "SELECT max(version) FROM run_state_machine WHERE NOT is_fixture")


async def carry_forward(conn, execution_id: int, actor: str = "operator"
                        ) -> dict:
    """Adopt the newest machine for this run. Forward only."""
    row = await conn.fetchrow(
        "SELECT state, machine_version FROM executions WHERE id = $1",
        execution_id)
    if row is None:
        raise CarryRefused(f"no execution {execution_id}")
    latest = await current_version(conn)
    # "Forward" is not a number comparison. A run can end up on a
    # FIXTURE machine — a test inserts version 900 to stay out of the
    # way — and 900 is numerically ahead of every real version while
    # being a fault rather than a future. Carrying such a run to the
    # current real machine is a repair, and refusing it on arithmetic
    # would strand it permanently.
    on_fixture = await conn.fetchval(
        "SELECT is_fixture FROM run_state_machine WHERE version = $1",
        row["machine_version"])
    if row["machine_version"] == latest:
        raise CarryRefused(
            f"execution {execution_id} is already on machine v{latest}, "
            f"the newest real one")
    if row["machine_version"] > latest and not on_fixture:
        raise CarryRefused(
            f"execution {execution_id} is on machine "
            f"v{row['machine_version']}, which is ahead of the newest "
            f"real machine v{latest}. Forward only.")

    # The run's CURRENT state must exist in the new machine. A version
    # that dropped a state would strand it somewhere the new rules
    # cannot describe, which is worse than the problem being solved.
    known = await conn.fetchval(
        "SELECT states @> to_jsonb($2::text) FROM run_state_machine "
        "WHERE version = $1", latest, row["state"])
    if not known:
        raise CarryRefused(
            f"machine v{latest} has no state {row['state']!r}, so this "
            f"run cannot be described by it")

    await conn.execute(
        "UPDATE executions SET machine_version = $2, updated_at = now() "
        "WHERE id = $1", execution_id, latest)
    logger.info("execution %s carried v%s -> v%s by %s", execution_id,
                row["machine_version"], latest, actor)
    return {"execution_id": execution_id, "action": "carried_forward",
            "from": row["machine_version"], "to": latest,
            "detail": f"machine v{row['machine_version']} -> v{latest}; "
                      f"the transitions it has already taken are "
                      f"unchanged, and the ones it may take now are "
                      f"today's"}
