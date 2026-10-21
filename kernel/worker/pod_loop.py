"""The leg loop, in the pod. D2's assembly, and the last of the cutover.

WHAT D2 LEFT. It built every part and started none of them: `leg_runner`
exists, `leg_lock` is held on the pod's own connection (D1), `loop_wire`
carries the result back, `loop_gate` refuses a frozen goal, the sqlproxy
sidecar is in the manifest and the model key reaches the worker. What
was missing is the thing that CALLS them, and its own note said so:
"a seam that looks finished is how a half cutover ships."

AND IT IS NOT NEW LOGIC. `advance_one` already does exactly one leg
under exactly one lock -- reachability, setup, the leg -- and its
docstring says it was factored out of `worker_tick` "so the serial and
concurrent paths run the SAME code; the alternative is two copies of
reachability, setup and leg, and the worker's own history (`worker_pass`
was a second `worker_tick` that drifted until three features were
unreachable from the console) is the argument against that."

That argument applies to this process with more force, not less. So
this module is a LOOP and a lifetime, and the leg itself is the same
function the conductor calls. A pod-side reimplementation would drift
the same way `worker_pass` did, and nobody would notice until a feature
worked from the console and not from a story.

WHY THIS FIXES THE STALL, which is the point of the whole phase. Today
the conductor runs every leg: a model turn, minutes long, holding one of
sixteen pooled connections, and `pool_safe_fanout` therefore caps it at
ten. Twenty concurrent stories cannot fit -- not at any setting, because
twenty legs cannot hold sixteen connections. Here each pod runs its own
story on its own 1-2 connection pool, and the conductor holds nothing:
its tick becomes indexed SQL and bounded Kubernetes calls, which is what
section 1 has claimed all along and what has not been true.

ONE EXECUTION, NOT A QUEUE. This pod was created FOR an execution and
its workspace claim is that execution's. It does not claim work from a
pool -- there is nothing for it to choose between, and a pod that could
pick up another story would be a second scheduler competing with
admission. It runs its own legs until the story is finished, paused, or
taken away from it.

THE LOCK IS STILL THE LOCK. `advance_one` takes `leg_lock` on the
connection it is given, and D1 put that connection in the pod precisely
so a conductor deploy cannot release a lock protecting a running leg.
Nothing here holds the lock across iterations -- it is taken and
released per leg, so a pause between legs is a pause that is seen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

logger = logging.getLogger(__name__)

# Which execution this pod belongs to. Set by the manifest builder from
# `spec.execution_id`, which is required there and has no None branch
# (plan 13.2) -- so its absence here is a bug in the manifest and not a
# state to handle politely.
EXECUTION_ENV = "AGENTOS_EXECUTION_ID"

# How long to wait before looking again when there was nothing to do.
# A leg that finished leaves the next one ready immediately; this is the
# interval for the cases where the row is not ready yet -- waiting on a
# human answer, a verification, or a pause.
IDLE_S = 4.0

# What a pod does when its own row says the story is over. It EXITS,
# rather than idling: the pod is the execution's, `reclaim` is what
# takes it away, and a worker that kept looping on a terminal row would
# hold its admission slot and its claim for as long as the pod lived.
TERMINAL = ("DONE", "FAILED", "CANCELLED", "ABANDONED")

# States in which a leg may be taken. Anything else -- VERIFYING,
# PARKED, DISPATCHING -- means somebody else's pass owns the row, and
# the honest thing is to wait rather than to race them for it.
RUNNABLE = ("RUNNING",)

# How many turns may raise IN A ROW before this pod gives up and exits.
#
# WHY GIVING UP IS THE SAFER FAILURE. The first draft never raised out of
# the loop -- "a leg that blows up is one leg" -- and that is right for a
# transient error and wrong for a broken deployment. A pod whose podfs
# answers is a pod the conductor PROMOTES: readiness passes, the
# execution goes RUNNING, and `report_stuck` only looks at PENDING and
# ADMITTED. So a loop that retried for ever held an admission slot on a
# story nothing would ever reap, looking healthy the whole time.
#
# Exiting turns that into a CrashLoopBackOff, which the pod never
# becoming Ready already handles: `promote_ready` gives up at
# STARTUP_BUDGET_S, destroys the pod and releases the execution with a
# reason. A loud failure the system knows how to clean up beats a quiet
# one it does not.
#
# CONSECUTIVE, not cumulative. A story that hits one bad leg an hour is
# working; a pod that has failed five in a row is not going to succeed
# on the sixth.
MAX_CONSECUTIVE_FAILURES = 5


class Unnamed(RuntimeError):
    """The pod does not know which execution it is. Refused loudly.

    A worker that guessed, or that scanned for work, would be a second
    scheduler racing admission. There is no safe default.
    """


def execution_id() -> int:
    """This pod's execution. Membership test, never a default.

    `kernel/` bans `environ.get(x, default)` and the reason applies
    exactly here: a default nobody can see would be an execution id
    nobody chose, and this process would drive somebody else's story.
    """
    if EXECUTION_ENV not in os.environ:
        raise Unnamed(
            f"{EXECUTION_ENV} is unset, so this worker cannot say which "
            f"execution it belongs to. The manifest builder sets it from "
            f"`spec.execution_id`, which is required there.")
    return int(os.environ[EXECUTION_ENV])


async def _row(conn, eid: int) -> dict | None:
    """This execution, as `advance_one` takes it."""
    found = await conn.fetchrow(
        "SELECT * FROM executions WHERE id = $1", eid)
    return dict(found) if found else None


async def local_bridge(conn, driver, row) -> int | None:
    """Where THIS pod's sandbox is, without asking the cluster.

    `ensure_reachable` -- the step this replaces inside the pod -- reads
    the pod object from the KUBERNETES API and may DESTROY it. Measured
    on execution 214: from inside the pod that call is routed through
    the egress proxy, which refuses it (`Tunnel connection failed: 403
    Forbidden`), and it would fail unproxied too because
    `agentos-execution` is bound to NO ROLE AT ALL. That is deliberate --
    it is the identity a model's `bash` shares a network namespace with,
    and giving it cluster read would undo the containment the whole pod
    design rests on.

    AND IT SHOULD NOT SUCCEED EVEN IF IT COULD. The probe is destructive:
    a pod that concludes its own sandbox is dead and deletes itself has
    no way to report why. Deciding a pod is unreachable is the
    conductor's job, from outside, where there is something left to
    write the reason on.

    THE ANSWER IS ALREADY KNOWN HERE. The sandbox is a container in this
    pod, on `k8s_spec.BRIDGE_PORT`, and the conductor ALREADY probed it:
    this execution is RUNNING because `promote_ready` got an answer on
    that port. Re-establishing it per leg would re-ask a question
    somebody else has answered.

    None is never returned. There is no "unreachable" outcome a pod can
    diagnose about itself -- if the bridge is genuinely dead the leg's
    first tool call fails and says so, with the execution still there to
    record it.
    """
    # `bridge.client`, which is where the port is DEFINED -- the
    # same constant the conductor's own probe dials.
    from kernel.bridge.client import BRIDGE_PORT             # noqa: PLC0415

    return int(row.get("bridge_port") or BRIDGE_PORT)


async def once(pool, driver, eid: int, run_leg_fn, executor_factory,
               adapter) -> tuple[list, bool]:
    """One turn of the loop. Returns (actions, keep_going).

    `executor_factory(conn)` RATHER THAN A PREBUILT EXECUTOR MAKER.
    `build_executor` is synchronous and takes the connection it will
    use, and this loop acquires a fresh one per turn -- so a maker
    built once would close over a connection that had gone back to
    the pool. `worker_pass` gets away with building it once because
    its closure is only a fallback: `leg_body` puts the leg's OWN
    connection on the row, and that is what the executor reads.
    Relying on that here would be relying on a fallback.

    A CONNECTION PER TURN, not one held for the pod's life. `leg_lock`
    is connection-scoped and `advance_one` takes and releases it around
    one leg, so holding a connection between legs would pin one of the
    pod's TWO for the time a human takes to answer a question. Two is
    not sixteen; the margin that made this invisible on the conductor
    does not exist here.
    """
    from kernel.worker.advance import advance_one           # noqa: PLC0415

    async with pool.acquire() as conn:
        row = await _row(conn, eid)
        if row is None:
            logger.warning("execution %s: no row; this pod has nothing to "
                           "drive", eid)
            return [], False
        state = str(row.get("state") or "")
        if state in TERMINAL:
            logger.info("execution %s: %s; the leg loop is done", eid, state,
                        extra={"event": "pod.loop"})
            return [], False
        if state not in RUNNABLE:
            # Somebody else's pass owns the row -- verification, a park,
            # a dispatch still settling. Waiting is not idleness; racing
            # them for it is how two writers disagree about one story.
            return [], True
        return await advance_one(conn, driver, row, run_leg_fn,
                                 executor_factory(conn), adapter,
                                 reachable=local_bridge), True


async def run_forever(pool, driver, run_leg_fn, executor_factory, adapter,
                      *, eid: int | None = None,
                      stopping: asyncio.Event | None = None,
                      idle: float = IDLE_S) -> None:
    """Drive this execution's legs until the story ends.

    NEVER RAISES OUT OF THE LOOP. A leg that blows up is one leg; a
    worker process that exits on it is a pod whose story stops with no
    verdict and no feedback, and `reclaim` would take minutes to notice.
    The same reason `dispatch_pass` catches per row.
    """
    eid = execution_id() if eid is None else eid
    logger.info("execution %s: the leg loop is running in the pod", eid,
                extra={"event": "pod.loop"})
    failures = 0
    while not (stopping and stopping.is_set()):
        try:
            actions, keep_going = await once(pool, driver, eid, run_leg_fn,
                                             executor_factory, adapter)
            failures = 0
        except Exception:                                   # noqa: BLE001
            failures += 1
            logger.exception("execution %s: the leg raised (%s in a row)",
                             eid, failures)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                # See MAX_CONSECUTIVE_FAILURES: exiting makes this a pod
                # the conductor already knows how to reap. Staying up
                # holds an admission slot for ever on a story nothing
                # watches.
                logger.error(
                    "execution %s: %s legs raised in a row; the worker is "
                    "exiting so the conductor reclaims this pod rather "
                    "than leaving it RUNNING and idle",
                    eid, failures)
                raise
            actions, keep_going = [], True
        if not keep_going:
            return
        # A YIELD ON EVERY TURN, including the busy one. The success
        # path loops straight back, and `once` only suspends because
        # `advance_one` does real I/O -- which is true in the cluster
        # and not true of a caller that fakes it. A loop that depends
        # on its callee to reach the event loop cannot be stopped, and
        # `stopping` is checked at the top of this loop: without this
        # the `stopping.set()` a shutdown performs is never seen.
        await asyncio.sleep(0)
        if not actions:
            # Nothing to do THIS turn. A leg that finished leaves the
            # next one ready at once, so an empty turn means the row is
            # waiting on somebody -- a human, a verification, a pause.
            if stopping is None:
                await asyncio.sleep(idle)
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=idle)
