"""How many sandboxes one pass may set up. Not how many legs may run.

WHAT WAS TRUE BEFORE. `dispatch_pass(..., limit)` defaulted to 4, and
its only caller passed `leg_concurrency()` — which is also 4. So one
number answered two unrelated questions, and it was sized for the
wrong one.

`leg_concurrency` is bounded by the CONNECTION POOL: a leg is a model
turn, it holds a connection for minutes, and its own module says so at
length ("the pool is 16 … ten leaves genuine headroom"). Dispatch holds
nothing. It creates a container and records an id — an API call each,
on one connection, with no model and no lock — and since the readiness
wait moved to `promote.py` that is the whole of it.

Sharing the leg number therefore rationed the cheap thing at the price
of the expensive one: a cold ramp could hand the substrate at most four
new sandboxes per pass however many were admitted and waiting.

TWENTY-FOUR, AND WHY THAT IS NOT A CEILING ON ANYTHING. Admission (C7)
decides how many runs may be in flight for a goal; this only decides
how many of the ones already admitted get their sandbox in one go. It
can never create work — there is nothing to dispatch that admission did
not already allow — so the cost of a high number is a longer pass, and
the pass is now bounded by N create calls rather than N container
startups.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

DEFAULT_DISPATCH_CONCURRENCY = 24

DISPATCH_CONCURRENCY_ENV = "AGENTOS_DISPATCH_CONCURRENCY"


def dispatch_concurrency() -> int:
    """How many executions one dispatch pass may take. Read per call.

    The default is spelled out rather than hidden in a two-argument
    `environ.get` — the house rule (`ci/check_no_environ_default.sh`
    enforces it over `kernel/`), whose reason is that a default nobody
    can see is a policy nobody reviewed.

    Nonsense falls back to the default rather than to 0: a typo in an
    environment variable must not silently stop the fleet dispatching,
    which is exactly what `0` or a negative number would do, quietly
    and for ever.
    """
    raw = (os.environ[DISPATCH_CONCURRENCY_ENV]
           if DISPATCH_CONCURRENCY_ENV in os.environ else "")
    raw = raw.strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    if raw:
        _log.warning("%s=%r is not a positive integer — using the default "
                     "of %d", DISPATCH_CONCURRENCY_ENV, raw,
                     DEFAULT_DISPATCH_CONCURRENCY)
    return DEFAULT_DISPATCH_CONCURRENCY
