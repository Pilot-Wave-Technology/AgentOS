"""One worker per execution. Enforced, not assumed.

`claimable` returns every RUNNING execution and nothing stopped two
callers acting on the same one. In E0 there are two: the console's
`_drive()`, started by a person clicking start, and the `_worker_forever`
sweep that exists so a run resumes after a park is answered. Both call
`run_one_leg`.

Observed live, on a story a person was watching: the console drove leg 2
while the sweep started leg 2, and the second one died on

    idempotency collision on key 'loop-31-gen-1-d0-step-0'

The key did its job — a silent double-run would have been far worse than
a loud crash — but a backstop is not mutual exclusion. Two models were
already calling tools against one workspace by the time it fired.

WHY AN ADVISORY LOCK RATHER THAN THE LEASE TABLE. `execution_leases`
exists and `dispatch` writes one, but that row means "this container owns
this execution" and is never released; it is not a per-leg token. A
second table-based lease would need staleness reclamation — pick a
timeout, heartbeat inside the leg, decide what a missed heartbeat means —
and every one of those is a way to either wedge a run or double-run it.
A Postgres advisory lock has none of that: it is held by a connection and
released when the connection goes, so a worker that crashes cannot leave
a run locked. The failure mode a table lease has to be careful about is
the one an advisory lock does not have.

Scoped to one connection: acquire and release MUST happen on the same
one, which is why this takes the connection the leg will use rather than
a pool.

WHERE THIS LIVES AFTER PHASE D. The leg moves into the execution pod,
and the paragraph above is why the lock cannot stay behind: it dies with
the CONDUCTOR'S connection, so a conductor deploy would release it while
the pod's leg ran on -- the double-run at the top of this module, with
the two callers further apart. So the pod gets a database connection of
its own; D1 built it (kernel/drivers/k8s_sqlproxy.py, and §10 of
docs/refactor/conductor_revamp_plan.md), and D2 is what passes that
connection to `leg_lock`. Nothing in this module changes: it already
takes a connection rather than finding one, which is the property that
makes the move a change of caller and not of design.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# A namespace of our own in the advisory-lock key space, so a lock here
# cannot collide with one taken by anything else sharing the database.
# 0x41474F53 — 'AGOS'. Fits in int4, which is what the two-argument form
# of pg_try_advisory_lock takes.
LOCK_CLASS = 0x41474F53

# ADJUDICATION GETS ITS OWN NAMESPACE, not the leg's. 0x41475644 --
# 'AGVD'. An execution is either legging or verifying and never both, so
# one class would work -- by accident rather than by design, which is
# the same argument `adjudicate_pass` makes for giving verification a
# supervisor of its own. Two classes also make `pg_locks` legible: a row
# says which pass is holding an execution, not merely that something is.
VERDICT_CLASS = 0x41475644


@asynccontextmanager
async def leg_lock(conn, execution_id: int):
    """Hold this execution's leg lock for the block. Yields whether we got it.

    Non-blocking on purpose. A worker that cannot get the lock has nothing
    useful to wait for — another worker is already taking the leg, and the
    right answer is to move on to the next execution and come back on the
    next tick.
    """
    held = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)", LOCK_CLASS, execution_id)
    try:
        yield bool(held)
    finally:
        if held:
            await conn.execute(
                "SELECT pg_advisory_unlock($1, $2)", LOCK_CLASS, execution_id)


@asynccontextmanager
async def verdict_lock(conn, execution_id: int):
    """Hold this execution's ADJUDICATION lock. Yields whether we got it.

    WHAT THIS CLOSES, and `adjudicate_pass` named it before it existed:
    "there is still no claim, no SKIP LOCKED and no advisory lock here,
    so two conductor processes both run the full sweep and the loser
    dies on a unique index."

    Both halves were measured on execution 203, claim 56. Two replicas
    each spent a criterion container reaching the same answer, and the
    second insert died:

        UniqueViolationError: duplicate key value violates unique
        constraint "verdicts_one_per_claim"
        DETAIL: Key (execution_id, claim_seq)=(203, 56) already exists.

    The unique index is the backstop and it did its job -- but a backstop
    is not mutual exclusion, which is the sentence at the top of this
    module about legs, now true of verdicts too. The cost of not having
    this is double the containers, double the model spend on assisted
    passes, and a coin toss over which verdict lands.

    The in-flight supervisor above cannot help: it is per PROCESS, and
    this is a race between processes.

    Non-blocking, for the reason `leg_lock` gives: the loser has nothing
    useful to wait for. It comes back next tick, finds the verdict
    written, and takes the `already` path.
    """
    held = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)", VERDICT_CLASS, execution_id)
    try:
        yield bool(held)
    finally:
        if held:
            await conn.execute("SELECT pg_advisory_unlock($1, $2)",
                               VERDICT_CLASS, execution_id)


async def is_locked(conn, execution_id: int) -> bool:
    """Is somebody holding this execution's leg lock right now?

    For tests and the console. Reads pg_locks rather than trying to take
    the lock, because trying would succeed for a session that already
    holds it — advisory locks are re-entrant per session, so a self-test
    by acquisition would always answer 'free'.
    """
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory'
              AND classid = $1 AND objid = $2 AND granted)
        """,
        LOCK_CLASS, execution_id))
