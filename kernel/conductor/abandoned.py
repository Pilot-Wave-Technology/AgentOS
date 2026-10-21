"""Runs whose reason for existing has gone. Cancel them, don't strand them.

THE REQUIREMENT: deleting a goal or a story must take its pods and its
verifiers with it.

Half of that already worked. `app/routers/goals.py` calls
`cancel_goal_runs` on delete, which submits a real `cancel` through the
mailbox -- so the run takes the edge at its next step boundary, the lease
is released, the admission slot comes back and the journal says what
happened. Writing `state = 'CANCELLED'` straight into the table would
skip all four and leave the machine disagreeing with its own log.

A CANCELLED STORY DID NOT. `stories.state = 'cancelled'` is written in
at least two places -- `git_graph/merge_lane.py` and
`routers/internal_assistant.py` -- and NEITHER touches the executions.
`reclaim.releasable` frees a workspace on `s.state IN ('done',
'reviewed')`, `e.state = 'CANCELLED'`, or FAILED-and-idle; `cancelled` is
none of them. So the story went terminal and its run kept its pod, its
PVC, its admission slot and its legs, with nothing anywhere saying why.

A SWEEP, NOT A PATCH AT EACH WRITER. There are two writers today and the
next one will not know to call this either -- that is exactly how the
goal path ended up with a teardown and the story path without one. A
sweep asks the question the writers keep forgetting to answer: is this
run's reason for existing still there? It is also the only form that
catches rows already stranded, which patching the writers never would.

TERMINAL IS READ FROM THE EXECUTION'S OWN MACHINE, for the reason
`orphans.py` gives: a run carried forward has different terminal states,
and judging one by another machine's is how a live run gets reaped.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

# A live execution whose story or goal is gone or finished with it.
#
# `stories` LEFT JOINed because an errand has no story and must not be
# swept for lacking one. `goals` likewise: a NULL goal_id is a fixture
# row, and `admit_pending` already refuses those loudly rather than
# guessing.
ABANDONED_SQL = """
    SELECT e.id, e.goal_id, e.story_id, e.state,
           CASE
             WHEN g.id IS NULL AND e.goal_id IS NOT NULL
               THEN 'its goal was deleted'
             WHEN s.state IN ('cancelled', 'rejected')
               THEN 'its story was ' || s.state
             ELSE 'abandoned'
           END AS why
      FROM executions e
      JOIN run_state_machine m ON m.version = e.machine_version
      LEFT JOIN stories s ON s.id = e.story_id
      LEFT JOIN goals   g ON g.id = e.goal_id
     WHERE NOT (m.terminal_states @> to_jsonb(e.state))
       AND (
            (e.goal_id IS NOT NULL AND g.id IS NULL)
         OR s.state IN ('cancelled', 'rejected')
           )
     ORDER BY e.id
     LIMIT 50
"""


async def cancel_abandoned(conn, actor: str = "system:abandoned") -> list:
    """Cancel every live run whose story or goal has gone. Returns actions.

    THROUGH THE MAILBOX, the same path the Cancel button uses. `cancel`
    is a LOOP_KIND with an edge from every non-terminal state: a running
    leg takes it at its next step boundary and a parked one ends on the
    next sweep. Everything downstream -- the pod, the claim, the
    admission slot, the criterion Jobs -- follows from the state change,
    so this function deliberately destroys nothing itself.
    """
    from kernel.mailbox.api import submit_proposal          # noqa: PLC0415

    actions = []
    for row in await conn.fetch(ABANDONED_SQL):
        try:
            await submit_proposal(
                conn, actor, "cancel",
                {"execution_id": row["id"], "reason": row["why"]},
                idempotency_key=f"abandoned:{row['id']}:"
                                f"{uuid.uuid4().hex[:12]}")
        except Exception as exc:                            # noqa: BLE001
            # One refusal must not stop the sweep: the others are
            # unrelated runs holding unrelated pods.
            logger.warning("could not cancel abandoned execution %s (%s): "
                           "%s", row["id"], row["why"], exc)
            continue
        logger.info("execution %s cancelled: %s", row["id"], row["why"],
                    extra={"event": "reconcile"})
        actions.append({"execution_id": row["id"], "action": "abandoned",
                        "detail": row["why"]})
    return actions
