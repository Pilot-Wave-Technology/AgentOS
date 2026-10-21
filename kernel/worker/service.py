"""The execution plane, as a process of its own.

WHAT THIS FIXES. `_worker_forever` -- admission, dispatch, legs,
adjudication, dry-runs, the errand schedule sweep, the usage drain, the
ledger drain and the credential sweep -- used to be started as a SIDE
EFFECT of `kernel/console/server.py:serve()` binding port 8770. So the
only way to run a story was to start a debug HTTP server, and three
separate consequences followed:

  - The one documented way to start it was a human typing
    `docker exec -d ... python3 -m kernel.console.server`. Forget it and
    the platform is indistinguishable from broken: stories sit at
    PENDING, every button returns 200, and nothing says why.
  - You cannot run two conductors when the loop only starts if an HTTP
    server claims a fixed port.
  - The console is an unauthenticated surface that should not be exposed
    -- and deleting it would have deleted the execution plane with it.

So the loop moves here and the console becomes what its own docstring
already claims it is: a view. It is now started by name:

    python -m kernel.worker.service

WHY asyncio.run AND NOT LoopRuntime. LoopRuntime exists because
`http.server` is threaded and asyncpg is not, so the stdlib servers need
a loop on a background thread to call into. A headless service has no
request threads and needs none of that -- it is one loop, owned by the
process, which is also what makes a SIGTERM handler possible.

──────────────────────────────────────────────────────────────────────

WHAT IS LEFT IN THIS FILE IS THE PROCESS, and only the process: the
entrypoint, the signal handlers, and the order of startup and shutdown.
It measured 299 code lines against the 200 ceiling (plan 11.5c/11.5f
D2a), and the three things inside it were each a different concern with
a different failure mode:

    service_tick    the pass itself -- `run_forever`, its three
                    cadences and its three separate `try` blocks
    service_pool    the database connection: waiting for it, bounding an
                    acquire, and letting go of it
    service_wake    how long a tick waits, and the NOTIFY that cuts the
                    wait short
    service_drain   letting the legs in flight finish, against the
                    grace period in deploy/k8s/conductor.yaml

THE NAMES ARE RE-EXPORTED because `kernel/verifier/service.py` imports
five of them (`DB_WAIT_S`, `INTERVAL_S`, `POOL_CLOSE_MAX_S`, `acquired`,
`drain_max_s`, `wait_for_pool`) precisely so the two services cannot
disagree about this deployment, and several tests import others. A
facade keeps every one of those true. `kernel.verifier.service`'s own
docstring says it MIRRORS this file rather than inventing a shape, and
that should stay readable as one import line.

STARTUP AND SHUTDOWN ORDER IS THE ONE THING THAT MUST STAY HERE:
`executors.install` BEFORE anything awaits, the drain BEFORE the pool
closes, and `executors.shutdown` AFTER it. Each of those was a
production incident.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from kernel import executors
from kernel.obs import alive, configure
from kernel.worker.service_drain import (DRAIN_DEFAULT_S, DRAIN_ENV,
                                         drain_max_s)
from kernel.worker.service_pool import (ACQUIRE_MAX_S, DB_RETRY_S, DB_WAIT_S,
                                        POOL_CLOSE_MAX_S, PoolBusy, acquired,
                                        close_pool, wait_for_pool)
from kernel.worker.service_tick import (RECONCILE_EVERY_TICKS,
                                        SWEEP_EVERY_TICKS, run_forever)
from kernel.worker.service_wake import (INTERVAL_S, PENDING_CHANNEL,
                                        listen_for_pending)

logger = logging.getLogger(__name__)

__all__ = [
    "ACQUIRE_MAX_S", "DB_RETRY_S", "DB_WAIT_S", "DRAIN_DEFAULT_S",
    "DRAIN_ENV", "INTERVAL_S", "PENDING_CHANNEL", "POOL_CLOSE_MAX_S",
    "PoolBusy", "RECONCILE_EVERY_TICKS", "SWEEP_EVERY_TICKS", "acquired",
    "close_pool", "drain_max_s", "listen_for_pending", "main", "run_forever",
    "wait_for_pool",
]


async def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    # BEFORE ANYTHING AWAITS. Every `to_thread` in this process lands in
    # the default executor, and until this runs that pool is sized from
    # the NODE's cpu count rather than this container's 1-CPU limit --
    # see kernel/executors.py for what saturated it and how the wedge
    # ended in a liveness kill.
    executors.install(asyncio.get_running_loop())
    pool = await wait_for_pool(dsn)

    stopping = asyncio.Event()

    def _stop(signame: str) -> None:
        # NAMED, because "the worker exited" and "the worker was asked
        # to exit" look identical in a log otherwise -- and on a Spot
        # node the difference is the whole diagnosis.
        logger.warning("worker service: %s received, draining",
                       signame, extra={"event": "tick"})
        # THE LIVENESS PROBE MUST NOT KILL THE DRAIN. The loops stop
        # ticking the moment this is set, and the drain may legitimately
        # run for `AGENTOS_DRAIN_MAX_S` (180s) under a 210s grace
        # period -- long enough for a probe watching for a wedge to fire
        # and SIGKILL the pod, discarding exactly the in-flight legs the
        # drain exists to save. See kernel/obs/alive.py.
        alive.draining(True)
        stopping.set()

    # ── READ THE STACK INSTEAD OF GUESSING AT IT ────────────────────
    #
    # A wedged conductor is two sleeping threads and a frozen CPU
    # counter: it tells you it is waiting and not what for. Diagnosing
    # that from outside has cost three rounds of speculation, and
    # `py-spy` cannot help -- the container drops the capability it
    # needs to ptrace.
    #
    # `faulthandler` can, from inside, for free: SIGUSR1 dumps every
    # thread's Python stack to stderr, which is already the log. It
    # allocates nothing until fired and cannot itself hang, because the
    # handler runs in the signal thread rather than on the event loop.
    #
    #     kubectl exec <pod> -c conductor -- kill -USR1 1
    #
    # SIGUSR1 rather than SIGQUIT or SIGABRT: those two kill the process,
    # which on a wedged pod destroys exactly the state being examined.
    try:
        import faulthandler                                  # noqa: PLC0415
        import signal as _signal                             # noqa: PLC0415

        if hasattr(_signal, "SIGUSR1"):
            faulthandler.register(_signal.SIGUSR1, all_threads=True,
                                  chain=True)
            logger.info("SIGUSR1 dumps every thread's stack "
                        "(kill -USR1 1)", extra={"event": "tick"})
    except Exception:                                       # noqa: BLE001
        # Never fatal. A conductor that cannot install a debugging aid
        # is a conductor that runs without one.
        logger.warning("could not register the stack dumper", exc_info=True)

    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _stop, signame)
        except NotImplementedError:
            # Windows has no add_signal_handler for the proactor loop.
            # Local development only; the containers are Linux.
            signal.signal(sig, lambda *_a, _n=signame: _stop(_n))

    try:
        await run_forever(pool, stopping=stopping)
    finally:
        await close_pool(pool)
        # AFTER the pool, and never blocking. A tool call still running
        # here is one whose caller is already gone; waiting for it would
        # hold the process past the grace period and turn a clean stop
        # into a SIGKILL -- which is the thing the drain exists to avoid.
        executors.shutdown()


if __name__ == "__main__":
    configure(component="worker")
    asyncio.run(main())
