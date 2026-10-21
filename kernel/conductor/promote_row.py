"""What the promote pass WRITES — the CAS, the lease, the reason, the undo.

EXTRACTED FROM `promote.py`, which was 203 code lines before this wave
and is capped at 200. The split is along a real seam and not an
arbitrary one: `promote.py` now decides (probe, verdict, budget, one
action record per row) and this executes (four statements, all against
one execution row). Nothing here calls the substrate except `destroy`,
and nothing in `promote.py` writes to the database except through here.

THE TRANSACTION IS SMALLER THAN THE ONE DISPATCH REFUSED TO HOLD.
`dispatch.py` runs outside a transaction because holding one across
`create()` + `await_ready()` pins a DB connection for the whole
container startup. Nothing here calls the substrate inside a
transaction: the probes happen in the caller, together, with no
connection held, and the transaction covers two statements against one
row.
"""

from __future__ import annotations

import logging

from kernel.conductor.unscheduled import NOTE_PATTERNS
from kernel.workspace.podfs_client import is_pod

logger = logging.getLogger(__name__)


async def claim(conn, execution_id: int, container_id: str,
                driver=None) -> int | None:
    """CAS to RUNNING, record the address, and take the lease — or nothing.

    Returns the generation the lease was written against, or None when
    the row was no longer a DISPATCHING row holding this container —
    two promoters racing, a cancel, or reconcile having taken it back.
    The zero-rowcount case is load-bearing: it is how the second
    promoter learns it lost, and it is why the lease cannot be written
    twice.

    THE LEASE IS CLAIMED BY THE CAS, NOT BEFORE IT. The old order was:
    read generation, INSERT the lease, then CAS the state. If the CAS
    lost — another replica, or reconcile recovering the row — the lease
    had ALREADY been written, against a generation read earlier still.
    This does the CAS first, in a transaction, and takes the generation
    from its RETURNING clause; the row lock is then held for the INSERT,
    so no generation can slip between the number and the lease that
    fences on it. A lost CAS writes nothing at all.

    AND IT CLEARS THE REASON. `note()` below writes on the row while a
    pod is still coming up — "the namespace refused this pod", "no node
    has room yet" — and a `stall_reason` left behind after the wait
    ended is worse than none: the run is RUNNING and the board still
    explains why it could not start. `admit_pending` makes exactly this
    point at its own PENDING → ADMITTED edge; this is the same rule at
    the next edge, and it is in the same statement as the CAS so a row
    can never be RUNNING with a stale excuse on it.

    `workspace_host` IS WRITTEN HERE, AND IT USED TO BE WRITTEN BY
    DISPATCH. Splitting readiness out of dispatch broke that, live, on
    execution 194:

        dispatch_failed: RuntimeError: pod exec-194 has no address yet,
        so its bridge cannot be reached.

    `dispatch_pass` resolved the address immediately after
    `dispatch_admitted`, which was safe only because dispatch used to
    block until the sandbox answered. It no longer does, so the pod had
    been created about a second earlier and had no IP.
    `resolve_bridge_host` refuses rather than guessing -- correctly,
    since a wrong address sends a leg's tool calls somewhere real -- and
    the refusal surfaced as a failed dispatch on a pod that was coming
    up perfectly well.

    Readiness is established by the caller, so the address is available
    here and nowhere earlier. Inside the same transaction as the CAS: a
    row that is RUNNING without an address is one the file explorer and
    every workspace crossing read as "this run has no pod".
    """
    async with conn.transaction():
        generation = await conn.fetchval(
            "UPDATE executions SET state = 'RUNNING', updated_at = now(), "
            "stall_reason = NULL, stalled_at = NULL "
            "WHERE id = $1 AND state = 'DISPATCHING' AND container_id = $2 "
            "RETURNING generation", execution_id, container_id)
        if generation is None:
            return None
        # Only a pod substrate has an address to record; on Docker the
        # tree is on this filesystem and empty means exactly that.
        if driver is not None and is_pod(driver):
            await conn.execute(
                "UPDATE executions SET workspace_host = $2 WHERE id = $1",
                execution_id,
                await driver.resolve_bridge_host(container_id))
        # holder is the container_id — the worker's identity for
        # heartbeat scoping (S4). A zero-rowcount heartbeat WHERE
        # holder = $holder AND generation = $gen means "you are fenced".
        await conn.execute(
            "INSERT INTO execution_leases (execution_id, holder, generation) "
            "VALUES ($1, $2, $3)", execution_id, container_id, generation)
    return generation


async def note(conn, execution_id: int, container_id: str, why: str) -> None:
    """Say ON THE ROW why this sandbox has not started. Never raises.

    §7b's admission invariant is "nothing waits without a
    `stall_reason`", and promotion is the loop where that was not true:
    a DISPATCHING row that is not answering produced an action record —
    a log line and an in-memory list — and wrote NOTHING. `stall_reason`
    is the column the board and the stall card read, and it is where an
    operator looks first, so a refusal that only ever reached a list was
    a refusal nobody saw until the 300-second budget expired and blamed
    the clock.

    AND IT NEVER TOUCHES `updated_at`. `promote._WAITING_SQL` measures
    the wait as `now() - updated_at`; a note that stamped it would reset
    the clock on every tick, the budget would never expire, and
    "report the refusal sooner" would become "wait for ever, politely".
    That is the whole reason this is not a copy of `reclaim`'s UPDATE,
    which stamps it deliberately because it is ending the wait.

    `IS DISTINCT FROM` because this is level-triggered: the pass runs
    every four seconds for as long as the pod is Pending, and rewriting
    the same sentence is one write per tick per pod for no reader.

    Best effort, like `admit._stalled`: promotion must not fail because
    the explanation could not be written.
    """
    try:
        await conn.execute(
            "UPDATE executions SET stall_reason = $2, "
            "stalled_at = COALESCE(stalled_at, now()) "
            "WHERE id = $1 AND state = 'DISPATCHING' AND container_id = $3 "
            "AND stall_reason IS DISTINCT FROM $2",
            execution_id, why[:500], container_id)
    except Exception:                                       # noqa: BLE001
        logger.warning("could not record why %s has not started",
                       execution_id, exc_info=True)


async def unnote(conn, execution_id: int, container_id: str) -> None:
    """Take back a reason that stopped being true. Never raises.

    THE OTHER HALF OF `note`, and the half that is easy to forget. A pod
    can be Unschedulable for ninety seconds and then PLACED, after which
    it pulls an image for two minutes more — and for those two minutes
    `stall_reason` still reads "no node has room for this pod yet",
    which is false, in the column an operator checks first. `claim`
    clears it, but only at RUNNING, which is exactly the moment nobody
    needed it cleared.

    OURS ONLY. The `LIKE ANY` is not a nicety: a DISPATCHING row can
    carry a dispatch failure (`runner.dispatch_pass`) or a contract
    refusal, and a blanket `stall_reason = NULL` here would erase those
    every tick — silencing two other loops to fix the silence in this
    one. `unscheduled.NOTE_PATTERNS` is generated from the very
    headlines `sentence()` writes, so the match cannot drift from what
    was written.

    It matches nothing on the ordinary path — a placed pod that never
    had a note — so this is a zero-row UPDATE per waiting row per tick,
    which is cheaper than the SELECT that would tell us to skip it.
    """
    try:
        await conn.execute(
            "UPDATE executions SET stall_reason = NULL, stalled_at = NULL "
            "WHERE id = $1 AND state = 'DISPATCHING' AND container_id = $2 "
            "AND stall_reason LIKE ANY ($3::text[])",
            execution_id, container_id, NOTE_PATTERNS)
    except Exception:                                       # noqa: BLE001
        logger.warning("could not clear the old reason on %s",
                       execution_id, exc_info=True)


async def lost(conn, driver, execution_id: int, container_id: str) -> dict:
    """The CAS lost. Decide whether anyone still owns this container.

    Two very different losses wear the same zero rowcount. Another
    promoter won, and the row now runs on THIS container — leave it
    alone. Or the row moved on without it (reconcile handed it back to
    ADMITTED, a cancel landed) and nothing claims the container any
    more: the orphan sweep will not take it either, because it skips any
    container whose execution is still live. So it is destroyed here,
    where we still know its id.
    """
    row = await conn.fetchrow(
        "SELECT state, container_id FROM executions WHERE id = $1",
        execution_id)
    if row is not None and row["container_id"] == container_id:
        return {"execution_id": execution_id, "action": "skip",
                "detail": f"already {row['state']} on {container_id[:12]}"}
    await destroy(driver, container_id)
    return {"execution_id": execution_id, "action": "reclaimed",
            "detail": f"sandbox {container_id[:12]} was orphaned by a "
                      f"concurrent change; destroyed"}


async def reclaim(conn, driver, execution_id: int, container_id: str,
                  why: str) -> dict:
    """Destroy the sandbox and leave the row DISPATCHING, as dispatch did.

    NOT back to ADMITTED. `dispatch.py` has said since C6 that returning
    it here makes a retry loop that never reaches the orphan sweep;
    reconcile owns the way back, and it advances the generation on the
    way, which fences anything the dead sandbox might still do.
    """
    await destroy(driver, container_id)
    await conn.execute(
        "UPDATE executions SET container_id = NULL, stall_reason = $2, "
        "updated_at = now() WHERE id = $1 AND state = 'DISPATCHING' "
        "AND container_id = $3", execution_id, f"sandbox: {why}"[:500],
        container_id)
    logger.warning("execution %s: sandbox %s reclaimed — %s", execution_id,
                   container_id[:12], why)
    return {"execution_id": execution_id, "action": "reclaimed",
            "detail": why}


async def destroy(driver, container_id: str) -> None:
    """Idempotent, and never the reason a pass fails."""
    try:
        await driver.destroy(container_id, stop_timeout=0)
    except Exception:                                       # noqa: BLE001
        pass
