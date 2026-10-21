"""Is the sandbox answering YET — a question, not a wait.

WHAT WAS TRUE BEFORE. `dispatch_admitted` called `driver.await_ready()`
inline, and `await_ready` is a LOOP with a budget: thirty seconds on
Docker, two minutes on Kubernetes, where a pod runs two init containers
and one of them clones repositories. A real sandbox took about 27
seconds to answer, and dispatch was called from a serial `for` loop —
so four sandboxes cost about 108 seconds, and for that whole time the
pass advanced nothing else: no leg, no park relay, no credential sweep,
no reconcile. Twenty stories from cold was about nine minutes of
conductor that looked wedged and was merely waiting.

Nothing about readiness ever required the caller to hold still for it.
A sandbox is either answering or it is not, and finding out costs one
round trip. This module asks, with a budget short enough to sit inside
a four-second tick, and returns a VERDICT rather than a container.

THREE ANSWERS, AND THE THIRD IS THE ONE THAT MATTERS. A sandbox that
EXITED is not merely unready — it is never going to be ready, and both
drivers say so distinctly: they raise `RuntimeError` carrying the
container's last words rather than timing out. Collapsing that into
"not yet" is how a dead pod sits in DISPATCHING for ever.

AN UNKNOWN ERROR READS AS "NOT YET". A 404 from a daemon that just
restarted, a transport blip, a driver refusing a keyword: none of those
is evidence that the sandbox is dead, and the caller's answer to GONE is
to destroy it. Leniency here is bounded — `promote.py` holds a startup
budget that reclaims a sandbox which never answers — and tearing down a
healthy pod because the network hiccuped is the far more expensive
mistake.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# How long ONE PROBE may take. This is not a startup budget: it is "are
# you up, right now", asked once a tick by a pass that has other work.
# Long enough for a refused connection and a daemon round trip, short
# enough that a tick full of unready sandboxes still ends promptly —
# they are probed together, so this bounds the pass, not each row.
PROBE_TIMEOUT = 1.0

READY = "ready"
WAITING = "waiting"
GONE = "gone"


async def bridge_port_of(driver, container_id: str) -> int | None:
    """The port this sandbox recorded, or None if nobody can say.

    READ, NOT RECOMPUTED — `ports.py` makes the point: whichever port a
    sandbox actually got is the one on its label, including when two
    allocations raced. Dispatch used to take it from the spec it had in
    hand; a pass that arrives later has no spec and must ask.

    `getattr` because a test double is a driver for exactly the calls it
    is asked to make, and several in this tree implement create/destroy
    and nothing else. An unlabelled sandbox answers None, which means
    the old readiness question (is it running) rather than the new one
    (is its bridge answering) — the same fallback `await_ready` already
    documents for the sleep containers that predate the shim.
    """
    resolve = getattr(driver, "resolve_bridge_port", None)
    if resolve is None:
        return None
    try:
        return await resolve(container_id)
    except Exception as exc:                                # noqa: BLE001
        logger.debug("no bridge port for %s: %s", container_id[:12], exc)
        return None


async def probe(driver, container_id: str,
                *, timeout: float | None = PROBE_TIMEOUT) -> tuple[str, str]:
    """Ask once. Returns (READY | WAITING | GONE, what happened).

    `timeout=None` means "the substrate's own startup budget" — the
    parameter is not passed at all, so Docker's thirty seconds and
    Kubernetes' two minutes each still apply. That is the shape dispatch
    used to have, kept for the one caller that genuinely wants to block:
    a person pressing start and watching for the sandbox.
    """
    port = await bridge_port_of(driver, container_id)
    kwargs: dict = {"bridge_port": port}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        answered = await driver.await_ready(container_id, **kwargs)
    except TimeoutError as exc:
        return WAITING, f"not ready yet: {exc}"
    except RuntimeError as exc:
        # Both drivers reserve this for "it exited before becoming
        # ready", and it carries the container's last words. That is a
        # verdict, not a delay.
        return GONE, str(exc)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("readiness probe for %s: %s: %s", container_id[:12],
                       type(exc).__name__, exc)
        return WAITING, f"probe failed: {type(exc).__name__}: {exc}"
    if answered is False:
        return WAITING, "the driver says not ready"
    return READY, (f"bridge answering on port {port}" if port
                   else "running (no bridge port recorded)")
