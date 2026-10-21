"""How many executions one admission pass may admit. C4.

WHAT WAS TRUE BEFORE. `admit_pending`'s select had no LIMIT. It was
bounded only by the per-(goal, domain, kind) bucket ceilings, which is a
ceiling on how many runs ONE bucket may hold and says nothing at all
about how many buckets there are. A system-wide burst therefore admitted
about forty executions in a single pass and dispatch provisioned about
forty pods, roughly thirty-six of which sat idle — each holding a PVC and
a node's worth of scheduling capacity while doing nothing.

THE BUCKETS ARE NOT WRONG, THEY ARE THE WRONG AXIS. A bucket answers "how
much of this goal's work may be in flight". Nothing answered "how much
work may the cluster take on at once", and the unbounded `FOR UPDATE SKIP
LOCKED` select also meant the number of rows one replica locked per pass
was whatever the queue happened to be.

WHY A BATCH BOUND RATHER THAN A GLOBAL IN-FLIGHT CEILING. Admission is
level-triggered and runs every tick, so a per-pass bound is already a
rate: the queue drains at N per tick and stops growing the instant
dispatch stops keeping up, because nothing that is still ADMITTED frees a
bucket slot. A global in-flight count would need a second source of truth
about what "in flight" means, and `admission_slots` already is one.

WHY 24. It is four ticks' worth of the default leg concurrency times the
default bucket ceiling — enough that a normal working day never sees the
bound at all, and small enough that the forty-pod burst becomes two
passes of twenty-four and twenty-four seconds of cluster back-pressure
instead of a PVC storm. It is configuration because the right number
depends on how big the cluster is, which this file cannot know.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

# Four ticks of four legs, which is the shape of a busy but healthy goal.
DEFAULT_ADMIT_BATCH = 24

ADMIT_BATCH_ENV = "AGENTOS_ADMIT_BATCH"

# A pass this large is not a ceiling, it is the absence of one — and the
# forty-pod burst was thirty-six idle pods, so "just raise it" is the
# thing that caused the incident. Past this, the answer is more replicas
# or a bigger bucket, not a bigger batch.
MAX_ADMIT_BATCH = 200


def admit_batch() -> int:
    """How many executions one admission pass may admit.

    Read per call, so a running conductor picks up a change without a
    restart — the same contract as `worker/concurrency.leg_concurrency`,
    whose idiom this follows exactly.

    The default is spelled out rather than hidden in a two-argument
    `environ.get`: the house rule over `kernel/`, whose reason is that a
    default nobody can see is a policy nobody reviewed.

    Nonsense falls back to the default rather than to 0 or to infinity. A
    typo must not silently stop admitting anything — which reads as "the
    system is wedged" — and must not silently restore the unbounded
    behaviour this exists to remove either.
    """
    raw = (os.environ[ADMIT_BATCH_ENV]
           if ADMIT_BATCH_ENV in os.environ else "")
    raw = raw.strip()
    if raw.isdigit() and int(raw) > 0:
        want = int(raw)
        if want > MAX_ADMIT_BATCH:
            _log.warning(
                "%s=%d exceeds the maximum of %d — using %d. A burst that "
                "big provisions pods faster than anything consumes them.",
                ADMIT_BATCH_ENV, want, MAX_ADMIT_BATCH, MAX_ADMIT_BATCH)
            return MAX_ADMIT_BATCH
        return want
    if raw:
        _log.warning("%s=%r is not a positive integer — using the default "
                     "of %d", ADMIT_BATCH_ENV, raw, DEFAULT_ADMIT_BATCH)
    return DEFAULT_ADMIT_BATCH
