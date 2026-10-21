"""One memory-writing turn at a time, per agent home.

`MEMORY.md` is written by the model: it reads the file and writes the whole
thing back. That is safe while an agent has one conversation. Since Stage K
an assistant's threads SHARE a workspace — one home, one `MEMORY.md` — and
two checkpoint turns overlapping lose one of them, silently, in the one turn
whose entire job is to record what happened.

**Why not split the file instead.** `MEMORY.md` is a bootstrap file: OpenClaw
injects a fixed allowlist of filenames into every session's system prompt
(`VALID_BOOTSTRAP_NAMES` in the gateway's `agents/workspace.ts`). A
`memory/<story>.md` is never injected — it is only findable by an explicit
read. Split the distilled facts across per-thread files and every thread
starts blind to what the others learned, which is the durability the shared
home exists to provide. The narrative IS split, because nothing reads it
automatically and it therefore loses nothing by being per-story.

So the file is shared on purpose, and the write is serialized instead.

**Keyed on the gateway agent id**, which is exactly the identity of a home.
For an agent with a gateway id per thread this is uncontended by
construction, so it costs those nothing and needs no flag to say which kind
of agent it is.

**A Postgres advisory lock, not an asyncio one**, because the control panel
can run as more than one replica and two replicas each holding their own
in-process lock is no lock at all. The cost is one pooled connection pinned
for the turn.
"""
from __future__ import annotations

import contextlib
import logging
import time

_log = logging.getLogger(__name__)

# How long a memory turn waits for another one in the same home. A flush is
# a single model turn; past this it is better to skip than to pin a
# connection and a caller behind something that may be wedged.
WAIT_S = 45.0
POLL_S = 0.5


def key_for(gateway_agent_id: str) -> str:
    return f"memhome:{gateway_agent_id}"


@contextlib.asynccontextmanager
async def held(gateway_agent_id: str):
    """Hold the home lock for the block, or yield False if it cannot be had.

    Yields True when the lock is held and False when it timed out — the
    caller decides what to do about it. It does NOT raise on timeout,
    because the callers are memory flushes and a flush that raises would
    propagate into a story transition.

    A missing pool, or any database failure, yields True: this is a guard
    against a race, not an authorisation check, and refusing to flush at all
    because the lock could not be taken would turn a rare lost line into a
    reliably lost turn.
    """
    from app.database import get_pool

    key = key_for(gateway_agent_id)
    try:
        pool = get_pool()
    except Exception:
        yield True
        return
    if pool is None or not gateway_agent_id:
        yield True
        return

    conn = None
    got = False
    try:
        conn = await pool.acquire()
        deadline = time.monotonic() + WAIT_S
        while True:
            got = bool(await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", key))
            if got or time.monotonic() >= deadline:
                break
            await _sleep(POLL_S)
        if not got:
            _log.warning(
                "memory home lock: timed out after %.0fs waiting on %s — "
                "another memory turn in this home is still running",
                WAIT_S, key,
            )
        yield got
    except Exception:
        _log.warning("memory home lock: unavailable for %s — proceeding unserialized",
                     key, exc_info=True)
        yield True
    finally:
        if conn is not None:
            if got:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtext($1)::bigint)", key)
                except Exception:
                    _log.warning("memory home lock: failed to release %s", key,
                                 exc_info=True)
            try:
                await pool.release(conn)
            except Exception:
                pass


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
