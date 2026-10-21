"""One reconciler at a time, across every conductor replica.

`deploy/k8s/conductor.yaml` runs `replicas: 2` and states why that is
safe: admission is `FOR UPDATE SKIP LOCKED`, leases are
generation-fenced, every transition is a CAS. The reconcile pass is the
one thing in the tick that none of those cover.

WHAT RACES. `reconcile_all` reads a keep-set -- every `container_id` any
execution still claims -- and `orphan_sweep` then destroys the live
containers not in it. Between the read and the destroy sits a
`driver.list_containers()` round trip to the substrate API, so the
keep-set is a SNAPSHOT, not a view. Replica A takes it; replica B admits
and dispatches an execution and writes its `container_id`; replica A
reaches the sweep, finds a container no row in ITS snapshot claimed, and
destroys the container a story just started running in. Nothing reports
that as an error, because destroying an unclaimed container is precisely
what the sweep is for -- it merely had a stale idea of what was claimed.
The demotions either side of it are CAS-guarded (`WHERE id = $1 AND
state = '...'`) and `driver.destroy` is idempotent; the keep-set is the
part with no guard of its own.

This cannot bite today only because `AGENTOS_RECONCILE_DELETE` is "0",
which makes the whole pass a dry run -- see `kernel/conductor/sweep.py`.
That flag exists to be set to "1", and the day it is, is the day a stale
keep-set stops being a counting error and starts killing runs. The lock
goes in first, which is the order `docs/refactor/decisions/
p3-cloud-workspace.md` records under "Open".

WHY AN ADVISORY LOCK. Same reasoning as `kernel/worker/lease.py`, which
this deliberately mirrors rather than inventing a second mechanism: the
lock is held by a CONNECTION and vanishes when the connection does, so a
conductor pod preempted mid-sweep -- a routine event on a rolling
update -- cannot leave reconciliation wedged for everyone. A table-based
leader row would need staleness reclamation, and every timeout picked
for that is either a window in which two replicas sweep anyway or a
window in which nobody does.

Scoped to one connection: acquire and release MUST happen on the same
one, which is why this takes the connection the pass will use rather
than a pool. A lock taken on a pool-borrowed connection while the sweep
queries another one guards nothing at all.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# A class of our own, distinct from `kernel/worker/lease.py`'s
# 0x41474F53 ('AGOS', the per-leg locks). Same trick -- four ASCII bytes
# packed into the int4 that the two-argument form of
# pg_try_advisory_lock takes -- so a row in pg_locks stays readable.
# 0x52434F4E is 'RCON'.
#
# A SEPARATE CLASS AND NOT A RESERVED OBJECT ID UNDER 'AGOS'. The second
# argument there is an execution id, and this pass has none; any
# constant it borrowed (0, -1) is one that a future caller passing a
# real execution id could collide with -- and the collision would be
# silent, expressing itself as a leg that will not start or a reconcile
# that never runs. A reconcile must be incapable of blocking a leg.
LOCK_CLASS = 0x52434F4E

# There is one reconcile pass, not one per goal or per anything, so the
# object id is a constant. Named rather than inlined so that whoever
# reads `classid 1380142926 | objid 1` out of pg_locks can grep for it.
LOCK_ID = 1


@asynccontextmanager
async def reconcile_lock(conn):
    """Hold the global reconcile lock for the block. Yields whether we got it.

    Non-blocking on purpose. A replica that cannot get the lock has
    nothing useful to wait for: the other replica is reconciling the
    same global state right now, so waiting buys a second pass over work
    that was just done. Worse, it would hold a pool connection for the
    length of somebody else's sweep -- and a sweep walks every container
    on the substrate. The next tick is a minute away
    (`RECONCILE_EVERY_TICKS` x `INTERVAL_S`) and reconciliation is
    hygiene, not the product.

    Released in a `finally` so a pass that raises -- and a sweep talks to
    a substrate API, so it can -- does not leave the lock held until the
    connection is recycled.
    """
    held = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)", LOCK_CLASS, LOCK_ID)
    try:
        yield bool(held)
    finally:
        if held:
            await conn.execute(
                "SELECT pg_advisory_unlock($1, $2)", LOCK_CLASS, LOCK_ID)


async def is_locked(conn) -> bool:
    """Is a replica reconciling right now?

    For tests and the console. Reads `pg_locks` rather than trying to
    take the lock, because trying would succeed for a session that
    already holds it -- advisory locks are re-entrant per session, so a
    self-test by acquisition would always answer 'free'.
    """
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory'
              AND classid = $1 AND objid = $2 AND granted)
        """,
        LOCK_CLASS, LOCK_ID))
