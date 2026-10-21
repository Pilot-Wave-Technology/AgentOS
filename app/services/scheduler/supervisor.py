"""Runs the background loops, and can say whether they are running.

WHAT THIS IS FOR, stated as the failure it exists to shorten. The park
card sweep raised on every pass for two and a half days. It logged
"kernel park cards: sweep failed" every five seconds throughout, and
nobody read it, because a log line that fires 43,000 times is
indistinguishable from weather. It was found when a person asked why one
plan card had not appeared.

Nothing could answer "is that loop working?" without reading logs. This
can.

TWO DIFFERENT FAILURES, AND THEY NEED DIFFERENT ANSWERS:

  A LOOP THAT DIES. Its task raises out of `while True` and is gone.
  Nothing restarts it, nothing notices, and it stays gone until the next
  deploy. `_run` below catches, records and restarts with backoff -- so
  a dying loop becomes a loop that limps and says so.

  A LOOP THAT LIVES AND ACHIEVES NOTHING. This is what actually
  happened: `run_forever` caught its own exception and carried on, so
  the task was perfectly healthy and did no work. Liveness supervision
  would NOT have caught it.

So a loop may call `beat()` when it completes a pass that did what it
was for. `last_ok` then means work, not breathing. A loop that never
beats reports `ok=None` -- honestly unknown rather than falsely green.
Opt-in on purpose: wiring every loop at once would be a large change
with no way to test it, and a beat added to a loop nobody doubts is
ceremony.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# Between one loop's start and the next. Twenty loops that all begin in
# the same millisecond take twenty pool connections in the same
# millisecond, on a pool of twenty, at the exact moment the process is
# also serving its first requests.
STAGGER_S = 0.4

# After a loop raises. Rises to a ceiling so a loop failing on something
# durable -- a missing table, a revoked grant -- retries at a rate that
# leaves the logs readable.
BACKOFF_START_S = 2.0
BACKOFF_MAX_S = 60.0


@dataclass(frozen=True)
class Loop:
    """One background loop. `factory` returns the coroutine to run.

    A factory rather than a coroutine because a coroutine can only be
    awaited once, and this restarts them. It also keeps the lazy imports
    the call sites already use: the import happens when the loop starts,
    not when this module is read.
    """

    name: str
    factory: Callable[[], Awaitable]
    why: str = ""


@dataclass
class _State:
    started_at: float = 0.0
    last_ok: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    restarts: int = 0
    running: bool = False
    beats: int = 0


class Supervisor:
    """Starts the loops and keeps a record of how they are doing."""

    def __init__(self) -> None:
        self._state: dict[str, _State] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    # ── running them ────────────────────────────────────────────────
    def start(self, loops: tuple[Loop, ...]) -> None:
        self._stop.clear()
        for i, loop in enumerate(loops):
            self._state.setdefault(loop.name, _State())
            self._tasks.append(asyncio.create_task(self._run(loop, i)))
        log.info("scheduler: supervising %d loops: %s",
                 len(loops), ", ".join(x.name for x in loops))

    async def _run(self, loop: Loop, index: int) -> None:
        await asyncio.sleep(index * STAGGER_S)
        state = self._state[loop.name]
        backoff = BACKOFF_START_S
        while not self._stop.is_set():
            state.started_at = time.time()
            state.running = True
            try:
                await loop.factory()
                # RETURNING IS NOT SUCCESS. These are `while True` loops;
                # one that returns has ended for a reason it did not
                # raise about, and restarting is the same answer as for
                # a crash.
                state.last_error = "loop returned; these are meant to run for ever"
                state.last_error_at = time.time()
            except asyncio.CancelledError:
                state.running = False
                raise
            except Exception as exc:                      # noqa: BLE001
                state.last_error = f"{type(exc).__name__}: {exc}"
                state.last_error_at = time.time()
                log.exception("scheduler: loop %s failed; restarting in %.0fs",
                              loop.name, backoff)
            state.running = False
            state.restarts += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    # ── saying how they are doing ───────────────────────────────────
    def beat(self, name: str) -> None:
        """A loop reporting that it completed a pass that did its job."""
        state = self._state.setdefault(name, _State())
        state.last_ok = time.time()
        state.beats += 1

    def status(self) -> dict:
        """For /healthz/loops. Ages in seconds, because an age is what a
        person reads -- a timestamp needs arithmetic before it means
        anything."""
        now = time.time()

        def age(then: float):
            return None if not then else round(now - then, 1)

        return {
            "loops": {
                name: {
                    "running": s.running,
                    "restarts": s.restarts,
                    # None = this loop does not report, so nothing here
                    # knows whether it is working. Not the same as False.
                    "ok_age_s": age(s.last_ok) if s.beats else None,
                    "beats": s.beats,
                    "error": s.last_error or None,
                    "error_age_s": age(s.last_error_at),
                }
                for name, s in sorted(self._state.items())
            },
        }


supervisor = Supervisor()
