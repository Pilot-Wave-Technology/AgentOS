"""The pass, every four seconds, until something says stop.

THIS IS THE EXECUTION PLANE. Admission, dispatch, legs, the credential
sweep and reconciliation all happen inside this loop, and until E-late
it was started as a SIDE EFFECT of the debug console binding port 8770 --
so the one documented way to run a story was `docker exec` on an HTTP
server. `kernel/worker/service.py` is the process that owns it now; this
file is what the process does.

THREE SEPARATE `try` BLOCKS, AND THAT IS THE DESIGN. The runs are the
product and the sweeps are hygiene; letting hygiene take down the
product is how a safety measure becomes an outage. Their consequence is
that a loop-level heartbeat would stay green while a pass inside it
failed silently for a week, which is why EACH pass writes its own
heartbeat row.

THE THREE CADENCES ARE NOT ARBITRARY, and none of them is a tuning knob:

  the worker pass        every tick. It is the product.
  the credential sweep   the NARROW form every tick, the full one every
                         75. F4: this used to run only every 75 ticks,
                         and a tick is a pass PLUS the sleep, so the
                         real period was 5-7 minutes against a 15-minute
                         TTL -- a finished run's credential sat on disk
                         for most of its life. The narrow form looks
                         only at runs that finished in the last few
                         minutes, so the common answer is an indexed
                         query returning nothing. The full form stays as
                         the backstop for what no exit path can cover: a
                         worker killed mid-run reaches no terminal
                         transition at all.
  reconcile              every 15. It compares the database against
                         every live container, and a run that has been
                         going four seconds has nothing to reconcile.
                         It COUNTS and does not delete until
                         AGENTOS_RECONCILE_DELETE is set -- wiring a
                         sweep that has never seen production data
                         straight to deletion would be trusting it on
                         the first tick it ever runs.

`stopping` IS WHAT MAKES THIS TESTABLE and what makes a clean shutdown
possible; the version this replaces looped on `while True` and could
only be killed.

ONE SUPERVISOR FOR THE LIFE OF THE LOOP. Rebuilt per sweep it would know
nothing about the legs the previous sweep started, which is the whole
reason it exists.
"""

from __future__ import annotations

import asyncio
import logging
import os

from kernel.obs import bound
from kernel.obs.heartbeat import beat
from kernel.worker.service_drain import drain_legs
from kernel.worker.service_pool import acquired
from kernel.worker.service_wake import (INTERVAL_S, listen_for_pending,
                                        wait_for_tick)

logger = logging.getLogger(__name__)

# The credential sweep's own, much slower cadence -- see the comment at
# its call site for why the narrow form runs every tick regardless.
SWEEP_EVERY_TICKS = 75

# RECONCILE CADENCE. Slower than the worker pass on purpose: reconciling
# compares the database against every live container, and a run that has
# been going four seconds has nothing to reconcile. The reconciler's tick
# rate and the admit/dispatch rate were always meant to differ -- see
# reconcile.py's own module docstring, which said so while nothing called
# it at any rate at all.
RECONCILE_EVERY_TICKS = 15


async def run_forever(pool, interval: float = INTERVAL_S,
                      stopping: asyncio.Event | None = None) -> None:
    """One pass every `interval`, until `stopping` is set.

    `stopping` is what makes this testable and what makes a clean
    shutdown possible; the version this replaces looped on `while True`
    and could only be killed.
    """
    from kernel.conductor.sweep import META_KEYS, reconcile_all
    from kernel.credentials.sweep import sweep as credential_sweep
    from kernel.drivers.registry import make as make_driver

    # S3. Set by the NOTIFY listener, cleared each time the tick acts on
    # it. An Event and not a queue: the payload says "look now", and two
    # inserts arriving between ticks should cause one pass, not two.
    woke = asyncio.Event()
    listener = await listen_for_pending(pool, woke)
    from kernel.harness.models import adapter_for_model
    from kernel.harness.models import choice
    from kernel.worker.executor_pass import worker_pass
    from kernel.worker.supervisor import LegSupervisor

    # A MODEL ID, NOT A PROVIDER, and this line said the opposite.
    # `get_adapter` takes 'gemini' or 'anthropic' and RAISES on
    # anything else, while `quick.ASSIST_MODEL` reads the SAME
    # variable as a model id defaulting to `gemini-3.8-flash`. So
    # setting it to the shape either one documented broke the other,
    # and nobody could choose a model at all.
    #
    # `adapter_for_model` is the entry point that resolves the seam
    # from the model -- "A user picks a MODEL. Which seam serves it
    # is our problem, not theirs."
    #
    # THIS IS THE DEPLOYMENT DEFAULT ONLY. A leg resolves its own
    # model from `executions.executor_model` (frozen at dispatch from
    # the story, then the goal); this adapter is what the tick builds
    # for everything that is not a leg.
    model = choice.default_model()
    driver, adapter = make_driver(), adapter_for_model(model)

    # FAIL HERE, NOT ONCE A MINUTE FOR EVER. A driver whose client is
    # missing is a deployment fault; raising before the loop means
    # `kubectl rollout status` fails and the deploy stops, instead of a
    # Ready pod logging ModuleNotFoundError every reconcile pass.
    driver.preflight()

    # ONE supervisor for the life of the loop. Rebuilt per sweep it would
    # know nothing about the legs the previous sweep started, which is
    # the whole reason it exists -- each leg logs its own actions as it
    # finishes.
    supervisor = LegSupervisor()
    # THERE WAS A SECOND, for the adjudications this tick used to spawn.
    # E12 moved the verdict to kernel/verifier/service.py, which keeps
    # its own; this process no longer starts one, so keeping an empty
    # pool to drain would be a drain of nothing on every deploy.
    stopping = stopping or asyncio.Event()
    tick = 0

    logger.info("worker service: driver=%s adapter=%s interval=%.1fs",
                type(driver).__name__, model, interval,
                extra={"event": "tick"})

    while not stopping.is_set():
        tick += 1
        with bound(tick=tick):
            # A ROW PER PASS. The two `try`s below are deliberate -- see
            # the sweep's comment -- and their consequence is that a
            # loop-level heartbeat would stay green while a pass inside
            # it failed silently for a week. So each pass writes its own.
            try:
                async with acquired(pool, "worker.pass") as hb_conn:
                    async with beat(hb_conn, "worker.pass",
                                    every=interval) as hb:
                        actions = await worker_pass(
                            pool, driver, adapter, supervisor)
                        hb.did(len(actions))
                for action in actions:
                    logger.info("worker: %s", action)
            except Exception:                               # noqa: BLE001
                logger.exception("worker pass failed")

            # SEPARATE try, deliberately. A failing sweep must not stop
            # the worker pass -- the runs are the product and the sweep
            # is hygiene, and letting hygiene take down the product is
            # how a safety measure becomes an outage.
            #
            # F4 -- EVERY TICK, NARROWLY. This used to run every 75
            # ticks, and a tick is a pass PLUS the sleep, so the real
            # period was 5-7 minutes against a 15-minute TTL: a finished
            # run's credential sat on disk for most of its life. The
            # narrow form looks only at runs that finished in the last
            # few minutes, so the common answer is an indexed query
            # returning nothing. The full form stays as the backstop for
            # what no exit path can cover: a worker killed mid-run
            # reaches no terminal transition at all.
            try:
                full = tick % SWEEP_EVERY_TICKS == 0
                async with acquired(pool, "sweep") as conn:
                    async with beat(conn, "worker.credential_sweep",
                                    every=interval) as hb:
                        swept = await credential_sweep(
                            conn, since_seconds=0 if full else 600)
                        # `sweep` returns {"checked", "released"}, not a
                        # list -- len() here would have counted the two
                        # dict keys and reported constant work forever.
                        hb.did((swept or {}).get("released", 0))
            except Exception:                               # noqa: BLE001
                logger.exception("credential sweep failed")

            # RECONCILE, and this is the first thing that has ever called
            # it. `reconcile_tick` has been written, tested and reachable
            # from nowhere but tests since it was built -- `grep
            # reconcile_tick app/ kernel/` returned only its own
            # definition. Its module docstring describes a tick rate for
            # a loop that did not exist.
            #
            # A THIRD separate try, for the same reason as the second:
            # hygiene must not take down the runs.
            #
            # It COUNTS and does not delete until AGENTOS_RECONCILE_DELETE
            # is set -- see kernel/conductor/sweep.py. Wiring it straight
            # to deletion would be trusting a sweep that has never seen
            # production data on the first tick it ever runs.
            try:
                if tick % RECONCILE_EVERY_TICKS == 0:
                    async with acquired(pool, "reconcile") as conn:
                        async with beat(conn, "worker.reconcile",
                                        every=interval * RECONCILE_EVERY_TICKS
                                        ) as hb:
                            summary = await reconcile_all(conn, driver)
                            # META_KEYS, not just "goals". `dry_run` and
                            # `skipped` are bools, and bool IS an int in
                            # Python -- excluding only "goals" counted
                            # `dry_run: True` as one unit of work, so a
                            # tick that reconciled nothing has always
                            # heartbeat "did 1".
                            hb.did(sum(v for k, v in summary.items()
                                       if isinstance(v, int)
                                       and k not in META_KEYS))
            except Exception:                               # noqa: BLE001
                logger.exception("reconcile pass failed")

        await wait_for_tick(stopping, woke, interval)

    # The listener held a connection for the life of the process; give it
    # back before the drain, so the pool close below is not waiting on a
    # connection nothing is using.
    if listener is not None:
        # NO `remove_listener`. It matches on the callback OBJECT, and a
        # fresh lambda here would never be the one that was registered --
        # it would fail silently and read as cleanup that happened.
        # Releasing the connection is what actually drops the listener,
        # because the pool resets it on the way back.
        try:
            await pool.release(listener)
        except Exception:                                   # noqa: BLE001
            logger.warning("could not release the listener connection",
                           exc_info=True)

    # ── LET THE LEGS IN FLIGHT FINISH ───────────────────────────────────
    # Bounded, because the grace period is real and SIGKILL is not
    # negotiable. kernel/worker/service_drain.py argues the number.
    await drain_legs(supervisor)
