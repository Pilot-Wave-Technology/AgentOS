"""A goal whose bucket has no slots must not be stuck for ever.

MEASURED ON `next` ON 2026-09-16, and it had been true for a long time.
Admission claims a row in `admission_slots`; `admission_buckets` only
records the ceiling. `ensure_bucket` guarded on the BUCKET:

    exists = await conn.fetchval(
        "SELECT 1 FROM admission_buckets WHERE ...")
    if exists:
        return

So a bucket whose slots were never written -- or were written and later
removed -- is unrepairable. The early return fires on every tick for
ever, `admit_pending` finds no free slot, and the execution sits at
PENDING with `stall_reason` NULL and nothing in any log. The function's
own docstring warns about precisely this outcome for a MISSING bucket
and then does not check the thing admission actually claims.

Eight of the thirteen buckets on `next` were in that state: console x2,
g3-baseline, h0w-live, h1c-slots and KER01 x3. The NCAL goals had their
slots, which is why it survived -- the goals in daily use worked, so the
fault looked like a property of old goals rather than of this function.

It cost a live dispatch to find. Execution 194 was created, the workspace
composed, the spec built, the profile resolved, the size chosen -- and
then nothing, silently, because a count nobody had reason to take was
zero.

WHY THESE TESTS USE A REAL SCHEMA DOUBLE rather than asserting on source
text: the defect was not a missing line, it was a guard on the wrong
table. Only running the two statements against a store that distinguishes
buckets from slots can tell those apart.
"""
from __future__ import annotations

import pytest

from kernel.conductor.admit import ensure_bucket

GOAL = "console"

# RE-KEYED BY OWNER. Buckets were (goal, domain, kind), which answered
# "how much coding work may this goal run" -- a question nobody asked.
# The budget belongs to WHO is running: the GO's autonomous errands get
# ten per goal, each person's assistant five. `domain` and `kind` are
# still columns, written as 'any', and no longer part of the identity.
OWNER = "assistant:someone"
CEILING = 5                          # what an assistant gets


class _Store:
    """Buckets and slots, with the real uniqueness of each.

    `admission_slots` is UNIQUE on (goal_id, owner, slot_index) and
    `admission_buckets` on (goal_id, owner) -- both verified against the
    live schema, which is what makes `ON CONFLICT DO NOTHING` a safe
    idempotent write rather than a way to accumulate duplicates.

    The old shape keyed both on (goal, domain, kind). With every owner
    now writing domain='any', that key made the SECOND owner in a goal
    collide with the first and -- because the inserts are ON CONFLICT DO
    NOTHING -- created zero slots in silence. Which is the exact defect
    this file exists to guard, arriving by a new route.
    """

    def __init__(self):
        self.buckets: dict = {}
        self.slots: set = set()

    async def execute(self, sql: str, *args):
        if "INSERT INTO admission_buckets" in sql:
            goal, owner, ceiling = args
            self.buckets.setdefault((goal, owner), ceiling)
            return "INSERT 0 1"
        if "INSERT INTO admission_slots" in sql:
            goal, owner, _ceiling, index = args
            self.slots.add((goal, owner, index))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected statement: {sql[:60]}")

    async def fetchval(self, sql: str, *args):
        if "count(*) FROM admission_slots" in sql:
            goal, owner = args
            return sum(1 for s in self.slots if s[:2] == (goal, owner))
        if "SELECT ceiling FROM admission_buckets" in sql:
            goal, owner = args
            return self.buckets.get((goal, owner))
        raise AssertionError(f"unexpected query: {sql[:60]}")

    def slot_count(self) -> int:
        return sum(1 for s in self.slots if s[:2] == (GOAL, OWNER))


# ── the defect ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_bucket_with_no_slots_gets_them():
    """THE ONE THAT WAS BROKEN. The bucket exists, so the old code
    returned immediately and the goal admitted nothing, for ever."""
    store = _Store()
    store.buckets[(GOAL, OWNER)] = 4          # bucket, no slots

    await ensure_bucket(store, GOAL, OWNER)

    assert store.slot_count() == 4, (
        "the bucket existed so the guard short-circuited, and a bucket "
        "with no slots admits nothing while looking fully configured")


@pytest.mark.asyncio
async def test_a_missing_bucket_still_gets_both():
    """The ceiling comes from the OWNER now, not from one global
    default -- an assistant gets five, the GO ten."""
    store = _Store()
    await ensure_bucket(store, GOAL, OWNER)
    assert store.buckets[(GOAL, OWNER)] == CEILING
    assert store.slot_count() == CEILING


@pytest.mark.asyncio
async def test_a_partial_set_of_slots_is_completed():
    """A ceiling raised after the fact, or an interrupted write."""
    store = _Store()
    store.buckets[(GOAL, OWNER)] = 4
    store.slots.add((GOAL, OWNER, 0))
    store.slots.add((GOAL, OWNER, 1))

    await ensure_bucket(store, GOAL, OWNER)
    assert store.slot_count() == 4


@pytest.mark.asyncio
async def test_it_is_idempotent():
    """Called every tick by the worker and by the console, and by two
    replicas at once. Running it twice must not double the slots."""
    store = _Store()
    await ensure_bucket(store, GOAL, OWNER)
    await ensure_bucket(store, GOAL, OWNER)
    await ensure_bucket(store, GOAL, OWNER)
    assert store.slot_count() == CEILING


# ── and it must not quietly narrow a goal somebody widened ───────────

@pytest.mark.asyncio
async def test_the_rows_ceiling_wins_over_the_argument():
    """The default comes from the owner -- five for an assistant. A goal
    deliberately widened to 10 must not be cut back to five by the next
    caller that did not think about it -- the ceiling is a decision, and
    it lives on the row."""
    store = _Store()
    store.buckets[(GOAL, OWNER)] = 10

    await ensure_bucket(store, GOAL, OWNER)      # default is 5

    assert store.slot_count() == 10
    assert store.buckets[(GOAL, OWNER)] == 10


@pytest.mark.asyncio
async def test_a_full_set_writes_nothing():
    """The ordinary case, every tick, for every goal. It must cost one
    count and stop -- not `ceiling` redundant INSERTs per tick."""
    store = _Store()
    store.buckets[(GOAL, OWNER)] = 4
    for i in range(4):
        store.slots.add((GOAL, OWNER, i))

    writes = []
    original = store.execute

    async def _counting(sql, *args):
        writes.append(sql)
        return await original(sql, *args)

    store.execute = _counting
    await ensure_bucket(store, GOAL, OWNER)

    assert not any("admission_slots" in w for w in writes), (
        "a satisfied bucket must not rewrite its slots every tick")
