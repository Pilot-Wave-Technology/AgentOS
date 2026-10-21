"""Legs outlive the sweep that started them.

THE HALF THAT WAS STILL SERIAL. Giving each leg its own connection made legs
claimed in the SAME tick run together, and that is genuinely all it did.
`_worker_forever` awaits `worker_pass` and only then sleeps, so while a batch of
legs is in flight no new sweep begins — and a run that becomes claimable one
second after the batch started waits for the whole batch to finish.

Observed on KER17, which is why this exists. Two stories, both approved, their
runs un-parking two seconds apart:

    13:06:01  1551 un-parks -> the next tick claims it ALONE
    13:06:03  1550 un-parks -> two seconds too late; the sweep is already
                               inside 1551's leg and will not return until
                               it ends
    ...       1550 takes its first step the instant 1551 parks

Two seconds of skew was enough to serialise them completely. The leg loop was
no longer the bottleneck; the sweep loop had become one.

WHAT THIS CHANGES. A sweep now DISPATCHES legs and returns. Each leg runs as a
task that outlives the pass, so the next tick — four seconds later — sees
whatever has become claimable since and starts it immediately, alongside
whatever is still running.

WHY IT IS SAFE TO SPAWN AND FORGET. `leg_lock` is a per-execution advisory lock
and already exists to arbitrate exactly this: `lease.py` opens by describing two
callers racing for one leg. A second attempt on a busy execution returns "busy"
rather than double-running. The in-flight set here is not the correctness
mechanism — it is how we avoid burning a connection and a claim to be told
something we already knew.

WHAT IS DELIBERATELY NOT HERE. No timeout that disciplines a SLOW leg. A leg
is a model call whose length is the model's business, and killing one mid-tool
leaves a sandbox in a state nobody described. The existing machinery covers
those failure modes: the leg ceiling ends a run that will not converge, the
breaker handles a provider that is down, and reconcile picks up an execution
whose worker died.

BUT A HUNG LEG IS NOT A SLOW ONE, and that distinction is what the paragraph
above missed. None of those mechanisms reclaim a leg that never returns: the
ceiling counts generations, a breaker cannot help a request that is still
open, and reconcile looks for a dead worker -- this one is alive and waiting.
Neither provider client sets a request timeout, so a socket that never answers
holds a slot AND a pooled connection for the life of the process.
`free_slots` never gives it back. Ten of those and the conductor is up,
ticking, heartbeating, and doing nothing at all.

So there IS a wall clock, and it is deliberately far past any legitimate leg
(`LEG_MAX_S`): it exists to reclaim a slot from a leg that has stopped
existing, not to hurry one that is working. It fires loudly, because a leg
that hit it is evidence of a hang somewhere below.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Far past any legitimate leg. `env_setup` alone may run 615s, a model
# call 600s before retries, and a leg is a model call plus several tool
# calls -- so this is not a budget, it is the line past which "still
# working" stops being a credible explanation.
LEG_MAX_S = 2700.0


def leg_max_s() -> float:
    """Read per call, so a soak can widen it without a restart."""
    if "AGENTOS_LEG_MAX_S" not in os.environ:
        return LEG_MAX_S
    try:
        return max(60.0, float(os.environ["AGENTOS_LEG_MAX_S"]))
    except ValueError:
        return LEG_MAX_S


class LegSupervisor:
    """The legs running right now, keyed by execution.

    One per worker process, held across sweeps — a supervisor rebuilt every
    pass would know nothing about the legs the previous pass started, which is
    the entire point of it.
    """

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}

    def in_flight(self) -> set[int]:
        """Execution ids with a leg running. Cheap; called every sweep."""
        return set(self._tasks)

    def free_slots(self, limit: int) -> int:
        """How many more legs may start. Never negative — a limit lowered
        between sweeps must not read as "start a negative number of legs"."""
        return max(0, limit - len(self._tasks))

    def spawn(self, execution_id: int, coro) -> bool:
        """Start a leg in the background. False if one is already running.

        The coroutine is closed on refusal rather than left unawaited: an
        un-awaited coroutine is a "never awaited" warning at garbage-collection
        time, which surfaces minutes later attached to nothing.
        """
        if execution_id in self._tasks:
            coro.close()
            return False

        task = asyncio.ensure_future(self._run(execution_id, coro))
        self._tasks[execution_id] = task
        return True

    async def _run(self, execution_id: int, coro) -> None:
        """Run one leg, log what it did, and always let go of the slot.

        Nothing raises out of here. A leg that fails in a way its own handler
        did not predict must not take down the supervisor, and must not leave
        its execution permanently marked in-flight — which would be a run that
        never moves again with nothing anywhere saying why.
        """
        try:
            for action in (await asyncio.wait_for(coro, leg_max_s())) or ():
                logger.info("worker: %s", action)
        except asyncio.TimeoutError:
            # LOUD. Nothing below here times out on its own, so reaching
            # this means a socket, a tool call or a provider stopped
            # answering -- and the slot and the pooled connection it was
            # holding are the reason the conductor looked busy while
            # doing nothing.
            logger.error(
                "worker: leg for %s exceeded %.0fs and was abandoned. "
                "Nothing under it has its own timeout, so this is a hang "
                "rather than slow work -- the slot and its database "
                "connection are being reclaimed.",
                execution_id, leg_max_s(), extra={"event": "tick"})
        except asyncio.CancelledError:
            logger.info("worker: leg for %s cancelled", execution_id)
            raise
        except Exception:                                   # noqa: BLE001
            logger.exception("worker: leg for %s failed", execution_id)
        finally:
            self._tasks.pop(execution_id, None)

    async def drain(self, timeout: float = 0.0) -> int:
        """Wait for the in-flight legs. Returns how many were still running.

        For shutdown and for tests. `timeout` of 0 means wait as long as it
        takes, because a leg interrupted halfway is a sandbox left mid-tool.
        """
        tasks = list(self._tasks.values())
        if not tasks:
            return 0
        await asyncio.wait(tasks, timeout=timeout or None)
        return len(tasks)
