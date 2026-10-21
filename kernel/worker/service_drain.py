"""Letting the legs in flight finish, bounded by the grace period.

`LegSupervisor.drain` was written for exactly this, says so in its own
docstring -- "For shutdown and for tests" -- and had NO CALLER. So
`stopping` only stopped new passes: the loop fell out, `main` closed the
pool, the process exited, and the event loop cancelled every in-flight
leg on its way down. Seen as

    worker: leg for 196 cancelled

A CANCELLED LEG IS A MODEL TURN ALREADY PAID FOR AND THROWN AWAY, and a
sandbox left mid-tool. It cost a deploy every time:
`terminationGracePeriodSeconds` was the only thing bounding this, so
every rolling update discarded whatever legs happened to be running.

BOUNDED, BECAUSE SIGKILL IS NOT NEGOTIABLE. A leg is one model turn plus
its tool calls -- seconds to a couple of minutes, almost all of it
waiting on a provider -- so 180 covers the ordinary leg and not the
pathological one. A leg past the bound is cancelled as before; the
difference is that the ordinary leg is not.

THE TWO NUMBERS ARE TIED AND ONE OF THEM IS IN YAML. `DRAIN_DEFAULT_S`
must stay BELOW `terminationGracePeriodSeconds` in
deploy/k8s/conductor.yaml, which is 210 for exactly this reason. If this
grows, that grows FIRST -- a drain that outlasts the grace period is a
drain that did not happen.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# HOW LONG SHUTDOWN WAITS FOR THE LEGS IT ALREADY STARTED. See the module
# docstring: 180 against a 210s grace period in conductor.yaml.
DRAIN_DEFAULT_S = 180.0
DRAIN_ENV = "AGENTOS_DRAIN_MAX_S"


def drain_max_s() -> float:
    """Read per call, and no two-argument `environ.get` -- the house rule
    over `kernel/` is that a default nobody can see is a policy nobody
    reviewed, and this one decides whether work is thrown away."""
    if DRAIN_ENV not in os.environ:
        return DRAIN_DEFAULT_S
    raw = os.environ[DRAIN_ENV].strip()
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number -- using %.0fs",
                       DRAIN_ENV, raw, DRAIN_DEFAULT_S)
        return DRAIN_DEFAULT_S
    if value < 0:
        return DRAIN_DEFAULT_S
    return value


async def drain_legs(supervisor) -> None:
    """Wait for the legs this process started, then say what it gave up on.

    The one-element loop is not an accident and not dead code: there
    were TWO pools here until E12 moved adjudication to
    kernel/verifier/service.py, and the shape is kept so that adding the
    next one is a tuple entry rather than a second copy of the bound,
    the log line and the warning.
    """
    for name, pool_of_legs in (("legs", supervisor),):
        remaining = len(pool_of_legs.in_flight())
        if not remaining:
            continue
        logger.info("worker service: draining %d %s (up to %.0fs)",
                    remaining, name, drain_max_s(), extra={"event": "tick"})
        left = await pool_of_legs.drain(timeout=drain_max_s())
        if left and pool_of_legs.in_flight():
            logger.warning(
                "worker service: %d %s still running after %.0fs; they "
                "will be cancelled. Raise %s, and terminationGracePeriod"
                "Seconds with it.", len(pool_of_legs.in_flight()), name,
                drain_max_s(), DRAIN_ENV, extra={"event": "tick"})
