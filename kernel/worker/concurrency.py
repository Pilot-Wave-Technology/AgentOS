"""How many legs run at once.

WHAT WAS TRUE BEFORE. `worker_tick` took up to `max_concurrent` claimable
executions per pass and awaited each leg in turn, on one connection:

    for row in (await claimable(conn, goal_id))[:max_concurrent]:
        actions.append(await run_one_leg(conn, ...))

So `WORKER_CONCURRENCY = 4` meant "advance four runs per sweep, one after
another", not "four legs at once" — a name that oversold what it delivered,
which is why nobody noticed. A leg is a model turn plus its tool calls: seconds
to minutes, almost all of it waiting on a provider. Serialising that makes N
stories cost N times the wall clock for work that was never CPU-bound.

The substrate was always ready for this. Per-execution advisory locks, generation
fencing, journal idempotency keys, CAS state transitions and per-sandbox bridge
ports all exist to arbitrate concurrent workers — `lease.py`'s docstring opens by
describing two callers racing for one leg. What was missing was a loop that used
any of it.

WHY THE CONNECTION IS THE UNIT. `leg_lock` is a Postgres advisory lock and its
own docstring is explicit: "Scoped to one connection: acquire and release MUST
happen on the same one." A leg holds its lock for the whole leg, so two legs on
one connection cannot both hold theirs — and an asyncpg connection refuses
concurrent use outright ("another operation is in progress"). Parallel legs
therefore mean a connection each, which is also what bounds this number.

THE CEILING IS NOT THIS NUMBER. Admission (C7) decides how many runs may be in
flight for a goal; this decides how many of their legs execute simultaneously.
They are different questions and conflating them is what kept the loop serial —
the old docstring declined to parallelise on the grounds that "the ceiling
belongs to admission, not to this loop inventing a second one", which is a
correct statement about ceilings and not an argument for running one leg at a
time. This number never admits anything; it only stops legs already admitted
from queueing behind each other.

D5 MADE THAT SENTENCE LOAD-BEARING. There used to be a second number here,
`MAX_LEG_CONCURRENCY = 10`, and two unrelated things read it as a ceiling:
`kernel/executors.py` sized the tool thread pool from it, and
`kernel/conductor/admit.py` described the system ceiling as sitting "below the
leg ceiling". Neither is true any more. E14 (plan §12) made every long bridge
call a JOB, so the pool is sized for polls; and after D the leg runs in its own
pod, where a per-conductor leg count means nothing at all. The single
documented ceiling on how much work the fleet runs at once is
`admit.SYSTEM_CEILING` — admission slots — and it is documented there, with its
cost.

WHAT SURVIVES IS A CLAMP, AND IT IS NOT A CEILING. `pool_safe_fanout()` below
exists only so that a typo in an environment variable cannot exhaust the
connection pool. Do not "simplify" it back into a policy number, and do not let
anything else import it to mean "how much the system may run": that is exactly
the dead-config path D5 closed — a knob nothing reads is how a future ceiling
gets set somewhere nobody looks.

D2 RETIRED THE CLAMP'S PREMISE, WHICH IS NOT THE SAME AS DELETING IT. 11.5i
asked D2 to retire `pool_safe_fanout()` on the grounds that "after D2 the
conductor does not run legs at all -- the pod does, on `LEG_POOL_MAX = 2`", so
a clamp on the conductor's fan-out has lost its subject. Measured, that is true
of ONE deployment shape and false of every shape that ships today.
`AGENTOS_EXECUTION_SQL_INSTANCE` is `off` in E0, on a laptop, under Docker and
in every test in this suite, and `off` is PERMANENT (see
`kernel/drivers/k8s_sqlproxy.OFF`, whose comment D2 corrected in the opposite
direction). In all of those the conductor still runs the legs, over a
16-connection pool, and deleting the clamp would have removed a LIVE guard from
every environment that exists in order to satisfy a sentence about one that
does not yet.

So what was retired is the premise, and the premise was the word POOL_MAX. The
old docstring below this said "`POOL_MAX` DELIBERATELY, not `pool_bounds()`
... reading the pod's bounds here would clamp a conductor that is not in a
pod", which was correct when no pod could fan anything out and is the exact
shape of a guard that is wrong precisely where it is needed. It now asks
`loopbridge.pool_bounds()` -- the pool THIS PROCESS actually built -- so the
answer is 10 in the conductor (unchanged, and `test_leg_concurrency` still
pins it) and 1 in the execution pod. One derivation, right in both, and
nothing left that has to know which process it is in.

AND THE DEFAULT IS CLAMPED TOO, which it was not. `leg_concurrency()` clamped
only a value a human had typed, so the pod would have taken
`DEFAULT_LEG_CONCURRENCY = 4` -- four `advance_one` calls, each holding one
pooled connection for the whole of reachability, setup and the leg -- over a
pool whose maximum is TWO. That is 11.5i's second inherited item and this is
where it is answered: not by shortening the hold, which C5 needs (see
`worker/advance.py`), but by making the number of simultaneous holders derive
from the pool they hold from. An unclamped default is a clamp that only works
for people who already knew about it.
"""

from __future__ import annotations

import asyncio
import logging
import os

_log = logging.getLogger(__name__)

# Four, because that is what the admission bucket seeds per (goal, domain,
# kind) and matching it means a goal at its ceiling is not also queueing
# behind this one. Raising it is a real decision with a real cost: see
# `pool_safe_fanout`, and `admit.SYSTEM_CEILING` for the actual ceiling.
DEFAULT_LEG_CONCURRENCY = 4

LEG_CONCURRENCY_ENV = "AGENTOS_LEG_CONCURRENCY"

# What the pool keeps back from this fan-out. Every in-flight leg holds one
# connection for the whole leg — minutes — and the sweep needs one of its own,
# as does the answerer, the reconciler and anything else sharing the pool. Six
# is what was reserved when this was written as a flat 10 against
# `POOL_MAX = 16`; it is stated as the RESERVATION now so that raising the pool
# raises the clamp with it, which a flat number did not.
#
# IT IS THE CONDUCTOR'S RESERVATION AND IT OVERSHOOTS IN THE POD, on purpose.
# The pod runs no sweep, no answerer and no reconciler, so six is far more than
# it keeps back — and `max(1, ...)` below turns the overshoot into a fan-out of
# one, which is what a process that was dispatched to run ONE leg should have
# had anyway. Tuning a second reservation for the pod would be a second number
# to keep in step with a pool that is already only two deep; the overshoot
# costs nothing because it cannot make the answer wrong in the safe direction.
_POOL_RESERVED = 6


def pool_safe_fanout() -> int:
    """The most legs THIS PROCESS may fan out over ITS OWN pool. NOT a ceiling.

    DERIVED RATHER THAN RESTATED, because the reason is entirely about the
    pool's size and a copy of a number drifts from its reason silently. A
    number that exhausts the pool does not fail loudly — it makes every OTHER
    query in the process wait on a connection, which reads as "the database
    got slow" and never as "somebody raised a concurrency setting".

    `pool_sizes()` AND NOT `POOL_MAX`, WHICH IS D2 REVERSING THIS FILE'S OWN
    REASONING. It said "`POOL_MAX` DELIBERATELY, not `pool_bounds()`. This
    fan-out belongs to the conductor's pass; the execution pod gets
    `LEG_POOL_MAX = 2` and runs the one leg it was dispatched for, so reading
    the pod's bounds here would clamp a conductor that is not in a pod." Both
    halves of that were true and the conclusion still did not follow: the two
    processes ask this question with the same code, and `loopbridge.
    pool_sizes()` is the ONE function that already knows which of them is
    asking — it is how `make_pool` sized the pool this clamp is about. Reading
    `POOL_MAX` instead does not protect the conductor, because `pool_sizes()`
    RETURNS `POOL_MAX` in the conductor; all it does is answer 10 in a process
    whose pool tops out at 2.

    (And `pool_bounds` is not a function. The sentence D2 replaced cited a
    name that has never existed in `loopbridge` — the argument for reading
    `POOL_MAX` rested on a reader not being able to look up the alternative.
    11.5f's rule about anchors that point at nothing applies to prose too.)

    The pod is not hypothetical here even though it does not fan out today.
    `conductor_leg_slots()` hands the pod zero slots (`worker/executor_pass`),
    so nothing currently reaches this in a pod at all — and a guard that is
    correct only while nothing reaches it is the guard that fails on the day
    something does. 11.5i predicted exactly that day.

    The second `max(1, ...)` matters as much as the derivation: a pool of two
    minus a reservation of six is negative, and a fan-out of zero is a process
    that admits work and then runs none of it, silently, which is the failure
    mode D5's own notes call the worst one available.

    Imported lazily: `loopbridge` pulls in the driver, and this module is
    imported by tests that have no database.
    """
    from kernel.loopbridge import pool_sizes                  # noqa: PLC0415

    return max(1, pool_sizes()[1] - _POOL_RESERVED)


def leg_concurrency() -> int:
    """How many legs may run at once. Read per call, so a run can change it.

    The default is spelled out rather than hidden in a two-argument
    `environ.get` — the house rule (`ci/check_no_environ_default.sh` enforces
    it over `kernel/`), whose reason is that a default nobody can see is a
    policy nobody reviewed.

    Nonsense falls back to the default rather than to 0 or to infinity: a typo
    in an environment variable must not silently stop the fleet, and must not
    silently exhaust the connection pool either.

    THE CLAMP IS RE-READ, not taken from the module constant, so a test (or a
    future pool resize) that moves `POOL_MAX` is honoured on the next call
    rather than at import time.
    """
    raw = (os.environ[LEG_CONCURRENCY_ENV]
           if LEG_CONCURRENCY_ENV in os.environ else "")
    raw = raw.strip()
    safe = pool_safe_fanout()
    if raw.isdigit() and int(raw) > 0:
        want = int(raw)
        if want > safe:
            _log.warning(
                "%s=%d exceeds the pool-safe fan-out of %d — using %d. This is "
                "a connection-pool clamp, NOT the concurrency ceiling: that is "
                "admit.SYSTEM_CEILING. Raise this process's pool first if you "
                "genuinely need more.",
                LEG_CONCURRENCY_ENV, want, safe, safe)
            return safe
        return want
    if raw:
        _log.warning("%s=%r is not a positive integer — using the default "
                     "of %d", LEG_CONCURRENCY_ENV, raw, DEFAULT_LEG_CONCURRENCY)
    # THE DEFAULT IS CLAMPED TOO, AND IT WAS NOT. Only a typed value was
    # checked, so a process whose pool cannot carry four legs took four
    # anyway as long as nobody had configured anything — which is every
    # process, because the variable is unset everywhere. In the execution
    # pod (`LEG_POOL_MAX = 2`) that is four holders over two connections
    # and the fourth waits on a connection that is not coming back until a
    # leg ends, minutes away. A default is a value somebody chose once for
    # one process; the clamp is a property of the process asking.
    return min(DEFAULT_LEG_CONCURRENCY, safe)


async def run_each_on_its_own_connection(acquire, rows, advance) -> list:
    """Advance every row at once, each on a connection of its own.

    Lives here rather than inline in `worker_tick` for two reasons. It is the
    whole of what "concurrent" means in this design — a connection each,
    gathered, with one row's failure isolated from the rest — and naming it
    makes that testable without importing the worker, which drags in the
    conductor and the model adapters and therefore an SDK that is not
    installed everywhere this suite runs.

    `acquire` is `pool.acquire`: a zero-argument callable giving an async
    context manager over a connection. `advance` is `(conn, row) -> list`.

    Returns one entry per row, IN INPUT ORDER (asyncio.gather guarantees it),
    each either the list `advance` produced or the exception it raised.
    Exceptions are returned rather than propagated because the rows are
    unrelated runs: one blowing up in a way nobody predicted must not cancel
    three others mid-model-call, which is precisely what a bare gather would
    do to them.
    """
    async def _one(row):
        async with acquire() as conn:
            return await advance(conn, row)

    return list(await asyncio.gather(
        *(_one(r) for r in rows), return_exceptions=True))
