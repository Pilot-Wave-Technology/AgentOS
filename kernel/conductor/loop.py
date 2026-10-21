"""Generic tick loop for the Conductor subsystem.

Ticks an async callable on a fixed cadence, emitting a marker line per tick
for observability. Stops cleanly on an asyncio.Event — no leaked tasks.

Used by D0 for reconcile_tick, reused by D9 for conductor_tick with a
different label and interval.
"""

from __future__ import annotations

import asyncio


async def run_loop(
    fn,
    interval: float,
    stop_event: asyncio.Event,
    label: str = "TICK",
) -> None:
    """Tick *fn* on *interval* until *stop_event* is set.

    Args:
        fn: An async callable returning a list (e.g. reconcile_tick bound
            via a closure).  The list length is reported in the marker line.
        interval: Seconds between ticks.
        stop_event: Setting this causes the loop to exit after the current
            sleep (or immediately if checked before a tick).
        label: Prefix for the marker line.  Convention: uppercase, e.g.
            ``RECONCILE_TICK``.

    The marker format is::

        {label} {n}: {k} actions

    where *n* counts from 1 and *k* is ``len(await fn())``.

    Returns when *stop_event* is set.  Does not raise.
    """
    n = 0
    while not stop_event.is_set():
        n += 1
        result = await fn()
        k = len(result)
        print(f"{label} {n}: {k} actions", flush=True)

        # Interruptible sleep: wait for either the interval to elapse or
        # stop_event to be set, whichever comes first.  This avoids
        # sleeping out the full interval after stop is requested.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            # Interval elapsed, stop_event not set — next tick.
            pass
        # If stop_event was set during the wait, the while condition
        # catches it on the next iteration.
