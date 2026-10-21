"""Deleting a goal ends the runs it left in flight.

WHAT `delete_goal` ALREADY DOES, and why it was not enough. It cancels
the goal's tasks and stories, fails its council runs, archives its
channels and tears down its gateway agents -- "the root fix; the
per-loop goals.status='active' guards are the complement". Its kernel
EXECUTIONS were not in that list, and they are the ones holding compute.

WHAT THAT COSTS, exactly. `reclaim.RELEASABLE_SQL` frees a workspace on
one of three signals: the story reaching `done` or `reviewed`, the
execution reaching `CANCELLED`, or a FAILED run going stale. Goal delete
produces none of them -- it writes `cancelled`, which is not `done` and
not `reviewed`, onto the STORY, while the execution stays RUNNING or
PARKED. So the pod keeps running and the claim keeps its disk, and the
namespace quota is `persistentvolumeclaims: "40"` for all executions
ever. A handful of deleted goals is the difference between a story that
starts and one that cannot be placed at all.

THROUGH THE MAILBOX, NOT A STATE WRITE. `cancel` is a LOOP_KIND with an
edge from every non-terminal state; a running leg takes it at its next
step boundary and a parked one ends on the next sweep. Writing
`state = 'CANCELLED'` directly would skip the lease release, the
admission slot and the journal entry, and leave the machine disagreeing
with its own log. The proposal is the supported path and it is the one
the story-thread Cancel button uses.

BEST EFFORT, PER RUN. One run that refuses must not stop the others: a
goal is being deleted, and a partially torn-down goal is the state this
exists to prevent.
"""

from __future__ import annotations

import logging
import uuid

_log = logging.getLogger(__name__)

# Mirrors kernel_run_control.TERMINAL -- imported rather than restated
# so the two cannot drift apart.


async def cancel_goal_runs(goal_id: str, actor_uid: str = "") -> dict:
    """Cancel every non-terminal run of a goal. Returns what it did."""
    from kernel.mailbox.api import submit_proposal

    from app.services.kernel_run_control import TERMINAL, _kernel_conn

    conn = await _kernel_conn()
    asked, refused = 0, 0
    try:
        rows = await conn.fetch(
            "SELECT id FROM kernel.executions "
            " WHERE goal_id = $1 AND state <> ALL($2::text[]) "
            " ORDER BY id", goal_id, list(TERMINAL))
        for r in rows:
            try:
                got = await submit_proposal(
                    conn, f"human:{actor_uid or 'system'}", "cancel",
                    {"reason": f"Goal {goal_id} was deleted."},
                    idempotency_key=(
                        f"cancel:{r['id']}:{uuid.uuid4().hex[:12]}"),
                    execution_id=r["id"])
                if isinstance(got, int):
                    asked += 1
                else:
                    refused += 1
                    _log.warning("goal %s delete: run %s refused cancel: %s",
                                 goal_id, r["id"],
                                 getattr(got, "message", got))
            except Exception:                               # noqa: BLE001
                refused += 1
                _log.exception("goal %s delete: cancel failed for run %s",
                               goal_id, r["id"])
    finally:
        await conn.close()
    _log.info("goal %s delete: %d run(s) cancelled, %d refused",
              goal_id, asked, refused)
    return {"cancelled": asked, "refused": refused}
