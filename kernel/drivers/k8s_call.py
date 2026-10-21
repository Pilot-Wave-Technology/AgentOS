"""Every Kubernetes API call in this codebase goes through here.

MEASURED. Both conductor pods wedged for thirty minutes. All three
loops frozen, `error_count = 0`, pods READY 2/2, nothing in the log.
`loop_heartbeats` showed `worker.pass` stale for 0:29:55 at tick 825.
Recovery was `kubectl rollout restart`, which is a human noticing.

TWO FAULTS, AND EITHER ALONE IS ENOUGH.

  1. NOTHING BOUNDED THE CALL. The generated Python client passes
     `_request_timeout` through to urllib3 and defaults it to None,
     which means no connect timeout and no read timeout. Zero of the
     thirteen call sites set it. A control plane that accepts the
     connection and then says nothing holds the caller for ever -- not
     an error, not a retry, just a call that never comes back.

  2. IT BLOCKED THE EVENT LOOP. Ten of the thirteen were called
     directly from async code. The client is synchronous, so one hung
     call does not stall one loop -- it stalls EVERY coroutine in the
     process. That is why all three loops froze together and why the
     symptom looked like a dead process rather than a slow API call.

Fixing one without the other buys little: bounded-but-blocking turns a
thirty-minute freeze into a thirty-second freeze of everything, and
non-blocking-but-unbounded leaks a thread per call until the pool is
gone. So both, at one seam, rather than thirteen hand-edits that the
fourteenth call site will not know about.

WHY NO `asyncio.wait_for` ON TOP. It would cancel the coroutine and
NOT the thread: `to_thread` has no way to interrupt work already
running. The thread would keep its slot in a pool of
`min(32, cpu + 4)`, and a belt that leaks a thread per firing
reproduces this outage by a slower route. `_request_timeout` is the
real bound because it makes the THREAD return; the tuple form bounds
the connect and the read separately, which is what covers both a
control plane that will not answer and one that will not accept.

There are no streaming calls in this codebase -- no `follow=True`, no
`kubernetes.stream` -- so a read timeout is safe at every site. The
bridge is delivered by an initContainer `cp`, not an exec stream. If a
watch is ever added it must not come through here.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os

logger = logging.getLogger(__name__)

# A control plane that will not complete a TCP handshake in this long is
# not slow, it is gone. Short on purpose: this is the one that catches a
# dropped SYN, which is the failure with no other symptom.
CONNECT_S = 5.0

# What an ordinary call may take. Overridable because a saturated
# Autopilot control plane is a real thing and a bound set too tight is
# its own outage -- spelled with the explicit `in os.environ` form
# because `kernel/` bans a default nobody can see.
DEFAULT_S = float(
    os.environ["AGENTOS_K8S_TIMEOUT_S"]
    if "AGENTOS_K8S_TIMEOUT_S" in os.environ else "30")

# Reading a container's whole log is the one call whose size is the
# work's business rather than the API's, so it gets its own budget.
LOGS_S = DEFAULT_S * 2


async def call(fn, *, budget: float = 0.0, **kwargs):
    """Run one Kubernetes API call off the event loop, bounded.

    Keyword arguments only, which every call site in this codebase
    already uses -- and which keeps `_request_timeout` from ever
    colliding with a positional.
    """
    kwargs["_request_timeout"] = (CONNECT_S, budget or DEFAULT_S)
    return await asyncio.to_thread(functools.partial(fn, **kwargs))


async def quiet(fn, *, what: str, budget: float = 0.0, **kwargs):
    """`call`, for the sites where failing is not fatal.

    Reading a pod's logs and deleting a finished Job are both like
    this: the exit code is the verdict and these are hygiene and
    evidence. They already swallowed their exceptions -- this keeps
    that behaviour and makes the swallow one decision instead of three
    copies of a bare `except Exception`.

    Returns None on failure, having said so at WARNING.
    """
    try:
        return await call(fn, budget=budget, **kwargs)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("%s failed: %s: %s", what, type(exc).__name__, exc)
        return None
