"""S3 -- the tick's clock, and the NOTIFY that can cut it short.

ADMISSION IS LEVEL-TRIGGERED AND POLLS. That is the contract and it does
not change here: the `interval` sleep is still the backstop, and a
conductor that cannot listen is a conductor that polls, which is exactly
how it worked before. This file only makes the sleep interruptible, so a
story admitted a moment after a tick does not wait out the whole of the
next one behind ten other passes.

AN EVENT AND NOT A QUEUE, deliberately. The payload says "look now" and
carries no work; two inserts arriving between ticks should cause ONE
pass, not two. That is also what makes a missed notification cost
latency and never an execution -- see migrations/execution_pending_notify.sql.

ITS OWN CONNECTION, HELD FOR THE PROCESS'S LIFE. `add_listener` binds to
a connection, so a pooled one handed back would stop listening the
moment somebody else used it. Releasing that connection is what actually
drops the listener; there is no `remove_listener` call anywhere, and
there must not be -- it matches on the callback OBJECT, so a fresh
lambda would never be the one that was registered and would fail
silently, reading as cleanup that happened.

`_first_of` is here rather than in a utilities module because it exists
for exactly one caller and one hazard: the LOSER of the race must not be
left pending across ticks. A task per tick that nobody awaits is a slow
leak and a shelf of "never retrieved" warnings at shutdown.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# How often a pass runs. Not a tuning knob so much as the resolution of
# every clock in the system that is expressed in ticks.
INTERVAL_S = 4.0
async def _first_of(*waits):
    """Return when the first of these does. Cancels the rest.

    `asyncio.wait` rather than `gather`: the point is the FIRST, and the
    loser must not be left pending across ticks -- a task per tick that
    nobody awaits is a slow leak and a shelf of "never retrieved"
    warnings at shutdown.
    """
    tasks = [asyncio.ensure_future(w) for w in waits]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
# S3 · admission is level-triggered and polls every `interval`. This makes
# it EVENTED as well: `AFTER INSERT ON executions` fires
# `pg_notify('execution_pending')` and the tick wakes immediately instead
# of waiting out its sleep behind ten other passes. See
# migrations/execution_pending_notify.sql.
PENDING_CHANNEL = "execution_pending"


async def listen_for_pending(pool, woke: asyncio.Event):
    """Hold one connection open on the channel. Returns it, or None.

    ITS OWN CONNECTION, held for the process's life: `add_listener` binds
    to a connection, so a pooled one handed back would stop listening the
    moment somebody else used it.

    NEVER FATAL. A conductor that cannot listen is a conductor that polls,
    which is exactly what it did before -- so this degrades to the old
    behaviour rather than refusing to start.
    """
    try:
        conn = await pool.acquire()
        await conn.add_listener(PENDING_CHANNEL,
                                lambda *_: woke.set())
        logger.info("listening on %s; admission wakes on insert",
                    PENDING_CHANNEL, extra={"event": "tick"})
        return conn
    except Exception:                                       # noqa: BLE001
        logger.warning("could not listen on %s -- admission falls back to "
                       "the %.0fs poll, which is how it worked before",
                       PENDING_CHANNEL, INTERVAL_S, exc_info=True)
        return None


async def wait_for_tick(stopping: asyncio.Event, woke: asyncio.Event,
                        interval: float) -> None:
    """Sleep until the next pass is due, or until something says look now."""
    try:
        # S3 · WOKEN EARLY BY A NEW EXECUTION. The sleep is still
        # here and still the backstop -- `woke` only shortens it.
        # A notification that never arrives (listener reconnecting,
        # a conductor restarting) must cost latency and never an
        # execution, which is why the payload carries no work.
        await asyncio.wait_for(
            _first_of(stopping.wait(), woke.wait()), timeout=interval)
        woke.clear()
    except asyncio.TimeoutError:
        pass
