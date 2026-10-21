"""The database connection, across the three moments it can go wrong.

WAITING FOR IT, BOUNDING AN ACQUIRE, AND LETTING GO OF IT. Three
different failures, one resource, and each of the three was a silent
one before it was fixed -- which is why they belong in the same file:
the shared lesson is that every wait on this pool must be bounded and
must say so out loud.

  wait_for_pool    the cloud-sql-proxy sidecar is STARTED before this
                   container but not yet listening. Under a second, and
                   enough: the process exited 1, Kubernetes restarted
                   it, it recovered every time, and so it looked free.
                   It was not -- every pod of every deploy carried
                   RESTARTS=1, and the post-rollout readiness check saw
                   a pod mid-restart and failed a deploy whose cluster
                   was healthy (deploy 08728e37).
  acquired         `pool.acquire()` has NO timeout of its own: it waits
                   for ever. With POOL_MAX 16 and unbounded consumers,
                   a saturated pool meant the tick simply stopped -- no
                   error, no heartbeat, and the liveness probe killed a
                   process that was patiently queueing. The wait was
                   fine; the SILENCE was the defect.
  close_pool       `Pool.close()` waits for every connection to be
                   released and something does not always release one.
                   Unbounded, the process sits until SIGKILL -- the full
                   grace period on EVERY pod of EVERY rolling update,
                   and raising that period to let legs drain made it
                   worse rather than better.

NONE OF THESE BOUNDS IS A TUNING KNOB. `DB_WAIT_S` is long enough for a
sidecar and short enough that an absent database fails the deploy;
`ACQUIRE_MAX_S` is longer than any pass should hold a connection;
`POOL_CLOSE_MAX_S` is deliberately SMALL, because by the time it is
reached the legs have drained and a connection still held is a leak
rather than work.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg

from kernel.loopbridge import make_pool

logger = logging.getLogger(__name__)

# And how long the POOL gets after that. Small on purpose: by this point
# the legs have drained, so a connection still held is a leak rather
# than work, and the alternative to severing it is sitting out the whole
# grace period on every pod of every deploy.
POOL_CLOSE_MAX_S = 15.0

# HOW LONG TO WAIT FOR THE DATABASE AT STARTUP. The cloud-sql-proxy
# sidecar is STARTED before this container but not yet listening, and
# the gap is under a second -- see `wait_for_pool`. Sixty is generous
# for that and still short enough that a genuinely absent database
# fails the deploy rather than leaving a pod up pretending.
DB_WAIT_S = 60.0
DB_RETRY_S = 1.0

# How long the tick will wait for a database connection before saying so.
# `pool.acquire()` has NO timeout of its own: it waits for ever. With
# POOL_MAX 16 and unbounded consumers (fixed in R3), a saturated pool
# meant the tick simply stopped -- no error, no heartbeat, and the
# liveness probe eventually killed a process that was patiently queueing.
# The wait itself is fine; the SILENCE was the defect.
ACQUIRE_MAX_S = 30.0


class PoolBusy(RuntimeError):
    """Every connection is held. Says which pass gave up, and for how long."""


@asynccontextmanager
async def acquired(pool, what: str):
    """`pool.acquire()`, bounded, so starvation is reported not endured.

    A context manager because that is how every call site uses
    `pool.acquire()`, and because the release has to happen even when the
    pass inside raises -- the one thing a saturated pool must not also
    suffer is a leaked connection.
    """
    # THE CONTEXT-MANAGER INTERFACE, not `await pool.acquire()`. asyncpg's
    # `PoolAcquireContext` supports both, but every call site here used
    # `async with` and so does every test fake -- awaiting it instead
    # broke them all with "object _Conn can't be used in 'await'
    # expression", which the build caught and my keyword-filtered local
    # run did not. Bounding `__aenter__` gets the timeout without
    # changing the interface anyone implements.
    ctx = pool.acquire()
    try:
        conn = await asyncio.wait_for(ctx.__aenter__(), ACQUIRE_MAX_S)
    except asyncio.TimeoutError as exc:
        raise PoolBusy(
            f"{what}: no database connection free after "
            f"{ACQUIRE_MAX_S:.0f}s. Something is holding one longer than a "
            f"pass should -- see AGENTOS_VERIFY_CONCURRENCY and "
            f"ANSWER_CONCURRENCY, which bound the two that used to be "
            f"unbounded.") from exc
    try:
        yield conn
    finally:
        await ctx.__aexit__(None, None, None)
async def wait_for_pool(dsn: str, budget: float = DB_WAIT_S):
    """The pool, once the database will take a connection.

    THE SIDECAR IS NOT LISTENING YET, AND THAT IS NOT AN ERROR. Cloud SQL
    arrives through `cloud-sql-proxy`, a native sidecar -- an
    initContainer with `restart: Always` -- so Kubernetes starts this
    container once the sidecar has STARTED, which is not the same as
    once it is accepting connections on 127.0.0.1:5432. The gap is under
    a second and it is enough:

        ConnectionRefusedError: [Errno 111] Connect call failed
        ('127.0.0.1', 5432)

    The process then exited 1 and Kubernetes restarted it. It recovered
    every time, so this looked free -- and it was not. Every pod of
    every deploy carried RESTARTS=1, and the `smoke` step that reads
    readiness straight after a rollout saw a pod mid-restart and failed
    a deploy whose cluster was healthy:

        ready flags: true
        false
        a conductor pod is not ready

    That is deploy 08728e37, and the conductor it was complaining about
    is serving correctly now.

    RETRYING HERE RATHER THAN ADDING A startupProbe TO THE SIDECAR: a
    probe would cover the first second and nothing else, and the proxy
    can be restarted at any point in a pod's life. A process that waits
    for its database is correct whenever the database is briefly away,
    which is a strictly larger set of moments.

    It is a WAIT, not a retry loop around the whole service: past the
    budget this raises, because a database that is not there after a
    minute is a deployment fault and a pod that stays up pretending
    otherwise is worse than one that fails.
    """
    deadline = asyncio.get_running_loop().time() + budget
    attempt = 0
    while True:
        attempt += 1
        try:
            return await make_pool(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            if asyncio.get_running_loop().time() >= deadline:
                logger.error("worker service: no database after %.0fs (%d "
                             "attempts): %s", budget, attempt, exc,
                             extra={"event": "tick"})
                raise
            logger.info("worker service: database not ready yet (%s); "
                        "retrying", type(exc).__name__,
                        extra={"event": "tick"})
            await asyncio.sleep(DB_RETRY_S)


async def close_pool(pool) -> None:
    """Close it, or sever it. Never sit on it until SIGKILL.

    `terminate()` is the right fallback and not a last resort: the legs
    have already drained by the time this is called, so anything still
    holding a connection is a leak. Severing it loses nothing a graceful
    close would have saved, and it says so in the log rather than
    looking like a slow shutdown.
    """
    logger.info("worker service: closing pool", extra={"event": "tick"})
    try:
        await asyncio.wait_for(pool.close(), timeout=POOL_CLOSE_MAX_S)
    except asyncio.TimeoutError:
        logger.warning(
            "worker service: pool did not close in %.0fs -- a "
            "connection was not released. Terminating it; the legs "
            "have already drained, so this severs a leak rather "
            "than work.", POOL_CLOSE_MAX_S, extra={"event": "tick"})
        pool.terminate()
