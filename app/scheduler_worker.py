"""The service that runs the background loops. One of it, always awake.

WHY A SERVICE OF ITS OWN, rather than a flag on the web tier.

Cloud Run gives a container CPU while it is serving a request and very
little between. That is right for a web app and fatal for a `while True`
loop: the loop only advances when something else happens to need the
instance. Measured on the git engine -- same code, same 20s interval --
2 ticks in ten minutes here against 30 in an environment deployed with
--no-cpu-throttling.

The control panel escaped that only by accident: a browser WebSocket is
a long-lived REQUEST, so an open tab keeps the loops alive. Close every
tab and twenty reapers stop, which is precisely when a reaper should be
working.

So this runs with CPU always allocated, min=1 and max=1. It serves one
route, and that route is not why it exists.

THE LOCK IS NOT REDUNDANT WITH max=1. During a revision rollover Cloud
Run runs the outgoing and incoming instances at the same time, so
max-instances=1 means "one in steady state", not "never two". A reaper
running twice for thirty seconds is long enough to delete something
twice. The advisory lock is what makes exactly-one true rather than
intended -- the same reasoning, and the same shape, as
`GitEngine._acquire_leader`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import asyncpg
from fastapi import FastAPI

from app.database import get_pool, init_db
from app.services.scheduler.registry import loops
from app.services.scheduler.supervisor import supervisor
from kernel.obs import configure as _obs_configure

# THE SCHEDULER WAS INVISIBLE, and that is worse here than anywhere.
# `app/main.py` calls this at import; this module had a bare `import
# logging` and configured nothing, so every line it wrote -- "scheduler
# worker started", "leadership acquired", every loop's own output --
# went nowhere. Cloud Logging showed uvicorn's three startup lines and
# then silence, for a service whose entire job is to run unattended.
#
# An hour was spent on the wrong question because of it: whether the
# card poller was running at all could not be answered from the logs,
# only inferred from whether cards appeared.
#
# AT IMPORT, before anything else can log, and matching the control
# panel's call so both services format the same.
_obs_configure(component="scheduler")

log = logging.getLogger(__name__)

# One number, chosen once, never colliding with another advisory lock in
# this database. `pg_advisory_lock` namespaces nothing for you.
LEADER_LOCK_KEY = 7_240_116_501

# How often a non-leader asks again. Short enough that a rollover hands
# over promptly, long enough not to be a poll storm.
LEADER_RETRY_S = 10.0

app = FastAPI(title="agentos scheduler")

_leader_conn: asyncpg.Connection | None = None
_leader_task: asyncio.Task | None = None


async def _hold_leadership() -> None:
    """Take the lock, start the loops, and keep the lock alive.

    A DEDICATED CONNECTION, held for the process's life. A
    session-scoped advisory lock lives exactly as long as its session,
    so it cannot come from the shared pool -- returning that connection
    would drop the lock silently and leave two schedulers believing they
    were alone.
    """
    global _leader_conn
    while True:
        try:
            if _leader_conn is None or _leader_conn.is_closed():
                _leader_conn = await get_pool().acquire()
                got = await _leader_conn.fetchval(
                    "SELECT pg_try_advisory_lock($1)", LEADER_LOCK_KEY)
                if not got:
                    # Another instance holds it -- a rollover, most
                    # likely. Let the connection go rather than pinning
                    # one per idle contender.
                    await get_pool().release(_leader_conn)
                    _leader_conn = None
                    await asyncio.sleep(LEADER_RETRY_S)
                    continue
                log.info("scheduler: leadership acquired; starting loops")
                supervisor.start(loops())
            # REVALIDATE, because an idle connection can be reaped by
            # Cloud SQL or the VPC -- and the lock dies with it while
            # this process still believes it leads. The git engine
            # learned this one the hard way; see its `_acquire_leader`.
            await _leader_conn.execute("SELECT 1")
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001
            log.warning("scheduler: leadership lost (%s); re-acquiring", exc)
            with contextlib.suppress(Exception):
                if _leader_conn is not None:
                    await get_pool().release(_leader_conn)
            _leader_conn = None
            await supervisor.stop()
        await asyncio.sleep(LEADER_RETRY_S)


@app.on_event("startup")
async def _startup() -> None:
    global _leader_task
    await init_db()
    _leader_task = asyncio.create_task(_hold_leadership())
    log.info("scheduler worker started (pid=%s)", os.getpid())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _leader_task is not None:
        _leader_task.cancel()
    await supervisor.stop()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "leader": _leader_conn is not None}


@app.get("/healthz/loops")
async def healthz_loops() -> dict:
    """What every loop is doing, as an age rather than a timestamp.

    This is the route the park-card outage wanted and did not have. A
    loop that has not reported in far longer than its interval is the
    question to ask, and until now the only way to ask it was to read
    two days of logs.
    """
    return {"leader": _leader_conn is not None, **supervisor.status()}
