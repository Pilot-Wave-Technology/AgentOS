"""Adjudication, as a process of its own. Phase E1, cut over at E12.

WHAT THIS IS AND WHAT IT IS NOT. This is the SCAFFOLDING for the split
described in docs/refactor/conductor_revamp_plan.md section 5: a second
Deployment, the same image, a different entrypoint. It changes no
adjudication logic and no batching -- E2 owns that -- and it does not
remove the conductor's verifier passes. Both processes run
`adjudicate_pass` while this is scaffolding, which is safe and is the
point:

  - `verdict_lock` (kernel/worker/lease.py) is a per-EXECUTION advisory
    lock, built in Phase R so two conductor replicas could not both
    spend a criterion container on one claim. Two DEPLOYMENTS are the
    same argument with a different noun, so cross-deployment exclusion
    is already true rather than something this has to invent.
  - The verifier PULLS its own work: `adjudicate_pass` polls
    `VERIFYING AND container_id IS NOT NULL`, so nothing has to hand
    anything over.
  - Feedback to the executor is a ROW, not a call -- `_tell_executor`
    submits a `steer` through the mailbox, read at the executor's next
    step boundary. That is I6, and it is what makes the boundary
    crossable at all.

So this Deployment could be rolled out, watched and rolled back without
anything depending on it. E12 then took the cutover: the conductor's tick
no longer adjudicates, and this process is the only one that does. The
dry-run pass that used to run beside it retired at E9 (plan 5.6) rather
than moving, so this loop has ONE pass.

WHY IT MIRRORS kernel/worker/service.py RATHER THAN INVENTING A SHAPE.
That module is the only headless entrypoint this codebase has, and every
paragraph in it is a fault someone paid for: the database that is not
listening for the first second of a pod's life, the drain that had no
caller and threw away paid-for model turns on every rolling update, the
pool that would not close, the stack dump that finally named the wedge.
Reusing them by IMPORT rather than by copy is deliberate -- a second
spelling of `wait_for_pool` would drift the way the two worker loops
drifted before `executor_pass.py` collapsed them.

WHAT IT DELIBERATELY DOES NOT DO.

  - No `reconcile_all`, no credential sweep, no orphan sweep. Reaping
    stays in the conductor. Plan 9.2 is explicit that the danger runs
    the other way -- the conductor's sweep listing `managed-by=kernel`
    is what destroyed a live capture Job (fault 4) -- and a SECOND
    process with the same reach would make that worse rather than
    better. W1's ownership rule is the fix, and it is not this task.
  - No NOTIFY listener. `execution_pending` fires on INSERT and is
    admission's wake-up; nothing notifies on the transition into
    VERIFYING, so the verifier polls. One tick of latency on a pass
    whose unit of work is a thirty-second Job is not where the time
    goes.
  - AND IT DOES PUBLISH, which an earlier version of this docstring
    denied. `finish_passed_claim` is reached from `verdict._apply` on
    the passing path and from the adjudicator's resume guard when a
    claim passed but the run never left VERIFYING -- both INSIDE
    `adjudicate_pass`. Adjudication moved here, so publish and the
    acceptance park moved with it: they were never anywhere else to
    stay. Plan 5.4 step 9b said "in the conductor" and was wrong;
    section 6 said podfs becomes "the verifier's client" and was right.
    The consequence is that this Deployment holds podfs WRITE and can
    push to the forge -- acceptable, since something must deliver and
    this is a trusted component, but it is a capability worth naming.

    python -m kernel.verifier.service
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from kernel import executors
from kernel.obs import bound, configure
from kernel.obs import alive
from kernel.obs.heartbeat import beat
# THE CONDUCTOR'S OWN LIFECYCLE, IMPORTED. `wait_for_pool` encodes the
# cloud-sql-proxy startup gap that cost a RESTARTS=1 on every pod of
# every deploy; `acquired` bounds `pool.acquire()`, which waits for ever
# by default and turned a saturated pool into a silently stopped tick;
# `drain_max_s` is read per call and is tied to
# `terminationGracePeriodSeconds` in deploy/k8s/conductor.yaml. All
# three are facts about this deployment too, and none of them should
# have a second implementation.
from kernel.worker.service import (DB_WAIT_S, INTERVAL_S, POOL_CLOSE_MAX_S,
                                   acquired, drain_max_s, wait_for_pool)
# WHICH GOALS THIS PROCESS VERIFIES. Imported rather than re-read so the
# verifier and the conductor cannot disagree about scope: a locally
# scoped stack that verified every goal while legging one would be a
# difference nobody would think to look for.
from kernel.worker.executor_pass import GOAL_SCOPE

logger = logging.getLogger(__name__)

# ONE NAME, and it is what the liveness probe judges. `kernel/obs/alive.py`
# keys a file per loop and compares each against its own declared
# cadence, so this string is the difference between "this process is
# wedged" and "nothing in this process has ever run".
#
# THIS PROCESS HAS EXACTLY ONE LOOP, which is why its pass duration is
# the whole of its liveness story -- there is no second loop ticking to
# cover for it. E3 is what let `deploy/k8s/conductor.yaml` bring the
# deadline back from 300s to the conductor's 120: the pass now spawns
# rather than awaits, so a pass is indexed SQL and `pool.acquire()`.
VERIFY_LOOP = "verifier.pass"


async def verify_pass(pool, driver, supervisor) -> list[dict]:
    """One sweep: the runs waiting on a verdict.

    IMPORTED, NOT REIMPLEMENTED. `adjudicate_pass` is the same function
    the conductor's tick used to call, and a refused story re-entering
    RUNNING is a behaviour of that function rather than of its caller --
    which is what made E12's cutover a deletion instead of a move.

    A ROW PER PASS, for the same reason the worker writes one: the
    `try` in `run_forever` means a pass can fail for a week under a
    green loop-level heartbeat.

    ONE SUPERVISOR, WHICH IS WHAT E9 LEFT OF E3. There were two -- a
    second for the dry run, with a budget of its own so a burst of
    adjudication could not starve a person waiting in front of an
    approval card. `DRYRUN_CONCURRENCY` existed ONLY because a dry run
    spawned Jobs, so it died with them; `VERIFY_CONCURRENCY` is now the
    whole of this process's budget.
    """
    from kernel.verifier.adjudicator import adjudicate_pass

    async with acquired(pool, VERIFY_LOOP) as conn:
        async with beat(conn, VERIFY_LOOP, every=INTERVAL_S) as hb:
            # `acquire` AND `supervisor`, which is what puts adjudication
            # on tasks of its own rather than inline -- see
            # `adjudicate_pass`'s docstring. Without both it degrades
            # to serial, and one slow suite would hold this loop for as
            # long as it ran -- which is what it used to do to the
            # conductor's whole tick, before E12 took it out of there.
            actions = await adjudicate_pass(conn, driver, GOAL_SCOPE,
                                            acquire=pool.acquire,
                                            supervisor=supervisor)
            hb.did(len(actions))
    return actions


async def run_forever(pool, interval: float = INTERVAL_S,
                      stopping: asyncio.Event | None = None) -> None:
    """One pass every `interval`, until `stopping` is set."""
    from kernel.drivers.registry import make as make_driver
    from kernel.worker.supervisor import LegSupervisor

    driver = make_driver()
    # FAIL HERE, NOT ONCE A TICK FOR EVER. A driver whose client library
    # is missing is a deployment fault: raising before the loop makes
    # `kubectl rollout status` fail, instead of a Ready pod logging
    # ModuleNotFoundError at every poll.
    driver.preflight()

    # ONE supervisor, for the life of the process. It keys on the
    # EXECUTION and refuses a second spawn for one already in flight, so
    # a four-second tick does not restart a ten-minute adjudication.
    supervisor = LegSupervisor()
    stopping = stopping or asyncio.Event()
    tick = 0

    logger.info("verifier service: driver=%s interval=%.1fs goal=%s",
                type(driver).__name__, interval, GOAL_SCOPE or "all",
                extra={"event": "tick"})

    while not stopping.is_set():
        tick += 1
        with bound(tick=tick):
            try:
                for action in await verify_pass(pool, driver, supervisor):
                    logger.info("verifier: %s", action)
            except Exception:                               # noqa: BLE001
                # THE LOG IS THE ONLY WITNESS NOW, and plan 9.2 says so:
                # "the diagnosis depended on the verifier's output being
                # in the CONDUCTOR's log. After E it will not be."
                logger.exception("verifier pass failed")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    await drain(supervisor)


async def drain(supervisor) -> None:
    """Let the work already in flight finish. Bounded.

    An adjudication holds a criterion Job and a pooled connection.
    Cancelling one throws away a container that has already been paid
    for and leaves a Job nothing is watching -- which the conductor's
    sweep then collects, or does not. The worker makes this argument
    about a model leg; a verification is the same sentence with a
    different noun, and the same bound applies because SIGKILL at the
    end of `terminationGracePeriodSeconds` is not negotiable.

    ONE POOL SINCE E9. E3 added a second for the dry runs -- once each
    was a task rather than inline, nothing waited for it, and a dry run
    was two Jobs and a person watching an approval card. The pass is
    gone, so the argument has nothing left to apply to.
    """
    for name, pool_of_tasks in (("adjudication(s)", supervisor),):
        remaining = len(pool_of_tasks.in_flight())
        if not remaining:
            continue
        logger.info("verifier service: draining %d %s (up to %.0fs)",
                    remaining, name, drain_max_s(), extra={"event": "tick"})
        await pool_of_tasks.drain(timeout=drain_max_s())
        if pool_of_tasks.in_flight():
            logger.warning(
                "verifier service: %d %s still running after "
                "%.0fs; they will be cancelled. Raise AGENTOS_DRAIN_MAX_S, "
                "and terminationGracePeriodSeconds with it.",
                len(pool_of_tasks.in_flight()), name, drain_max_s(),
                extra={"event": "tick"})


def _dump_stacks_on_sigusr1() -> None:
    """SIGUSR1 writes every thread's Python stack to stderr, which is the
    log. THE SAME REGISTRATION kernel/worker/service.py makes, and for
    the same reason: a wedged process tells you it is waiting and not
    what for, `py-spy` cannot attach because the container drops the
    capability it needs to ptrace, and the liveness probe fires this
    (`alive.dump_the_wedged_process`) on its way to failing -- so the
    dump lands seconds before the kill, with nobody watching.

    SIGUSR1 and not SIGQUIT or SIGABRT: those kill the process, which on
    a wedged pod destroys exactly the state being examined.

    Duplicated rather than shared because the conductor's entrypoint is
    owned elsewhere this week; when Phase D next touches it, the two
    should become one helper.
    """
    try:
        import faulthandler                                 # noqa: PLC0415

        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, all_threads=True,
                                  chain=True)
            logger.info("SIGUSR1 dumps every thread's stack (kill -USR1 1)",
                        extra={"event": "tick"})
    except Exception:                                       # noqa: BLE001
        # Never fatal: a process that cannot install a debugging aid is
        # a process that runs without one.
        logger.warning("could not register the stack dumper", exc_info=True)


def _on_signal(stopping: asyncio.Event, signame: str) -> None:
    logger.warning("verifier service: %s received, draining", signame,
                   extra={"event": "tick"})
    # THE LIVENESS PROBE MUST NOT KILL THE DRAIN. The loop stops ticking
    # the moment this is set and the drain may legitimately run for
    # AGENTOS_DRAIN_MAX_S -- long enough for a probe watching for a
    # wedge to fire and SIGKILL the pod, discarding the very work the
    # drain exists to save. See kernel/obs/alive.py.
    alive.draining(True)
    stopping.set()


def _install_signal_handlers(loop, stopping: asyncio.Event) -> None:
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _on_signal, stopping, signame)
        except NotImplementedError:
            # Windows has no add_signal_handler for the proactor loop.
            # Local development only; the containers are Linux.
            signal.signal(sig, lambda *_a, _n=signame: _on_signal(stopping,
                                                                  _n))


async def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    # BEFORE ANYTHING AWAITS. Every `to_thread` in this process lands in
    # the default executor, and until this runs that pool is sized from
    # the NODE's cpu count rather than this container's limit. This
    # process is the one Phase R sized `infra=64` for: tar parsing, git
    # archive and Kubernetes calls are all threaded here.
    executors.install(asyncio.get_running_loop())
    pool = await wait_for_pool(dsn, DB_WAIT_S)

    stopping = asyncio.Event()
    _dump_stacks_on_sigusr1()
    _install_signal_handlers(asyncio.get_running_loop(), stopping)

    try:
        await run_forever(pool, stopping=stopping)
    finally:
        logger.info("verifier service: closing pool", extra={"event": "tick"})
        try:
            # BOUNDED. `Pool.close()` waits for every connection to be
            # released, and a cancelled adjudication is exactly the case
            # where one is not -- unbounded, the process then sits until
            # SIGKILL on every rolling update.
            await asyncio.wait_for(pool.close(), timeout=POOL_CLOSE_MAX_S)
        except asyncio.TimeoutError:
            logger.warning(
                "verifier service: pool did not close in %.0fs -- a "
                "connection was not released. Terminating it; the "
                "adjudications have already drained, so this severs a "
                "leak rather than work.", POOL_CLOSE_MAX_S,
                extra={"event": "tick"})
            pool.terminate()
        # AFTER the pool, and never blocking. A `to_thread` still running
        # here is one whose caller is already gone.
        executors.shutdown()


if __name__ == "__main__":                              # pragma: no cover
    configure(component="verifier")
    asyncio.run(main())
