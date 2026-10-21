"""The worker container: the workspace surface AND the leg loop.

`k8s_workspace.worker_command()` ran `kernel.workspace.podfs` alone, and
that is the last thing standing between D2's parts and D2's cutover --
`executor_pass.conductor_leg_slots()` returns zero the moment
`AGENTOS_EXECUTION_SQL_INSTANCE` leaves `off`, so without something here
to take the legs, the fleet admits, dispatches and never runs one.

TWO JOBS, ONE CONTAINER, AND THEY ARE NOT THE SAME KIND OF THING.

  podfs      a `ThreadingHTTPServer` on 9101. It serves the CONDUCTOR
             (workspace reads, the commit surface) and, since Phase E1,
             the VERIFIER -- which is why `execution-bridge-ingress`
             admits 9101 from `app: agentos-verifier` and 9100 from
             nobody but the conductor. It must be up whenever the pod
             is, including while no leg is running.
  the leg    an asyncio loop driving THIS execution's legs through
             `advance_one`, on the pod's own 1-2 connection pool.

EITHER DYING TAKES THE CONTAINER DOWN, and that is the whole reason this
module exists rather than a `sh -c "a & b"`. The failure that shape
hides is the expensive one: podfs's thread dies, the leg loop carries
on, and the pod looks healthy while the conductor cannot read its
workspace and the verifier cannot fetch its tree. Every symptom lands
somewhere else -- a verification that cannot get a tree, a commit that
never surfaces -- and none of them names this pod. A container that
exits is a `CrashLoopBackOff` somebody reads within the hour, which is
the argument `podfs.main` already makes for refusing to start without a
token.

THE SURFACE COMES UP FIRST. The leg's own `ensure_reachable` probes the
sandbox, not podfs, so the ordering is not a correctness requirement
between these two -- but the conductor promotes this pod by probing
BOTH containers, and a leg that started before the surface answered
would be a leg running inside a pod the conductor still calls
DISPATCHING.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

# How long to give podfs to bind before the leg loop starts. Not a
# correctness barrier -- see the docstring -- so it is short, and a
# surface that has not bound by then is one the readiness probe will
# fail anyway.
SURFACE_GRACE_S = 2.0


def _serve_surface(stop: threading.Event, bind: str) -> threading.Thread:
    """podfs, in a thread that sets `stop` when it ends for any reason.

    NOT A DAEMON THREAD. A daemon would let the process exit while the
    surface was still serving, and -- worse -- would let the surface die
    without anybody noticing, which is exactly the half-worker this
    module exists to prevent.
    """
    def run():
        try:
            from kernel.workspace import podfs               # noqa: PLC0415
            podfs.main(["--bind", bind])
        except BaseException:                               # noqa: BLE001
            logger.exception("podfs stopped; the worker cannot serve its "
                             "workspace, so the container must go down")
        finally:
            stop.set()

    thread = threading.Thread(target=run, name="podfs", daemon=False)
    thread.start()
    return thread


async def _drive(stopping: asyncio.Event) -> None:
    """The leg loop, on the pod's own pool."""
    import os                                               # noqa: PLC0415

    from kernel.drivers.registry import make as make_driver  # noqa: PLC0415
    from kernel.harness.loop import run_leg                  # noqa: PLC0415
    from kernel.harness.models import adapter_for_model      # noqa: PLC0415
    from kernel.harness.models import choice                 # noqa: PLC0415
    from kernel.loopbridge import make_pool                  # noqa: PLC0415
    from kernel.worker import pod_loop                       # noqa: PLC0415
    from kernel.drivers.k8s_sqlproxy import leg_dsn          # noqa: PLC0415
    from kernel.worker.executor_pass import build_executor   # noqa: PLC0415

    # THE DEPLOYMENT DEFAULT, and only that -- the LAST resort of three.
    # `_take_leg` calls `adapter_for_execution` per leg, which prefers
    # `executions.executor_model` (a person's frozen choice) and then
    # `profile.model`, and falls back to this adapter only when neither
    # names one.
    #
    # THIS COMMENT USED TO CLAIM THAT ALREADY and it was not true:
    # it named `choice.for_execution`, which had NO CALLER. The UI
    # wrote the columns, dispatch froze them, and every leg ran on this
    # default regardless.
    #
    # `adapter_for_model`, not `get_adapter`: the latter takes a
    # PROVIDER and raises on a model id, and this line used to hand
    # it a variable that means a model id everywhere else.
    adapter = adapter_for_model(choice.default_model())

    def executor_factory(conn):
        def make_executor(profile, row, plan_approved=None):
            return build_executor(profile, row, plan_approved, conn=conn,
                                  adapter=adapter, actor_prefix="worker")
        return make_executor

    # ASSEMBLED HERE, NOT READ. `worker_env` hands the pod the
    # socket host as a value and username/password/dbname as
    # SECRET REFERENCES; a DATABASE_URL written by the manifest
    # would have to interpolate the password and turn a
    # secretKeyRef into a literal in the pod spec.
    pool = await make_pool(leg_dsn(os.environ))
    try:
        await pod_loop.run_forever(
            pool, make_driver(), run_leg, executor_factory, adapter,
            stopping=stopping)
    finally:
        await pool.close()


async def _main(bind: str) -> int:
    stop = threading.Event()
    stopping = asyncio.Event()
    _serve_surface(stop, bind=bind)

    # Bridge the thread's event onto the loop's, so podfs dying stops
    # the leg loop rather than leaving it running in a pod that can no
    # longer be read.
    async def watch_surface():
        while not stopping.is_set():
            if stop.is_set():
                logger.error("podfs is gone; stopping the leg loop")
                stopping.set()
                return
            await asyncio.sleep(0.5)

    await asyncio.sleep(SURFACE_GRACE_S)
    watcher = asyncio.create_task(watch_surface())
    try:
        await _drive(stopping)
    except BaseException:                                   # noqa: BLE001
        # MEASURED ON EXECUTION 212, and this module's own docstring
        # claimed the opposite. `_drive` builds the pool BEFORE
        # `run_forever`, so a DSN that cannot authenticate raises out
        # here rather than into the loop's retry -- and the podfs
        # thread is NOT a daemon, so the interpreter waits for a
        # `serve_forever` that never returns. The process hung instead
        # of exiting: worker `ready=true`, `restarts=0`, podfs serving,
        # and no leg loop at all. That is precisely the silent
        # half-worker this file exists to prevent, one level above the
        # place I prevented it.
        #
        # `os._exit` AND NOT `raise`. Raising leaves the same thread
        # holding the process up; `sys.exit` only unwinds this thread.
        # Only `_exit` takes the container down, which is what makes it
        # a CrashLoopBackOff the conductor's promotion path already
        # knows how to reap.
        logger.exception(
            "the leg loop stopped; taking the container down so the "
            "conductor reclaims this pod rather than leaving it Ready "
            "with no leg running")
        stopping.set()
        watcher.cancel()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    finally:
        stopping.set()
        watcher.cancel()
    # The leg loop returning means the story is over. The surface is
    # still serving and the pod is still this execution's until
    # `reclaim` takes it -- so the container stays up rather than
    # exiting into a restart nobody asked for.
    logger.info("the leg loop is finished; the workspace surface stays up")
    while not stop.is_set():
        await asyncio.sleep(1.0)
    return 1


def main(argv=None) -> int:
    """`--bind`, passed through to podfs and meaning what it means there.

    `0.0.0.0` because the conductor is a different pod and there is no
    loopback route to this one; the reach is restricted by
    `execution-bridge-ingress`, not by the bind address. Parsed rather
    than ignored: `worker_command()` passes it, and a flag silently
    dropped is a surface bound somewhere nobody expects.
    """
    import argparse                                          # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="kernel.worker.pod_main")
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_main(args.bind))


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
