"""LT1 — admission, run against a real schema.

WHY THIS FILE EXISTS. Admission broke twice on 2026-09-17, and **both
times CI was green**:

  * eight of thirteen buckets had zero slots. Silent, and it had been
    true for days.
  * the cross-goal ceiling claimed a SECOND slot per execution, which
    `admission_slots_one_per_execution` forbids. `UniqueViolationError`
    came out of `admit_pending` on every tick, so admission stopped
    **entirely, for every goal**. Measured live on execution 205.

Every existing admission test reads the SOURCE — substrings, an AST walk,
a call ordering. Those are worth having and they caught real things, but
not one of them can see a unique index, because the index exists only in
the schema. The fakes hand back a slot id for any `fetchval`, so the
second claim looked like a success.

So this file does the one thing none of the others do: it puts real rows
in a real Postgres, runs the real pass, and asserts the real transition.
Run it against the local stack; SKIP when no database is reachable.
Every test rolls its transaction back — the dev database is never
mutated.

`asyncio.run`, not `@pytest.mark.asyncio`: pytest-asyncio is NOT
installed here, and an async test without it ERRORS rather than skipping.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from kernel.conductor import admit                            # noqa: E402

# THE OWNER ADMISSION WILL ACTUALLY RESOLVE for these fixtures, which
# is not a free choice. `admit_pending` attributes each row with
# `owner.resolve`, and a fixture execution has no story -- so it comes
# back UNKNOWN. A bucket built under any other name would be the wrong
# bucket, and every one of these tests would fail for a reason that has
# nothing to do with what it is testing.
from kernel.conductor.owner import UNKNOWN as OWNER

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def _dsns() -> list[str]:
    seen, out = set(), []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip():
    tried = []
    for dsn in _dsns():
        try:
            return await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "kernel,public"}),
                timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip("no local database — start local-development/scripts/"
                f"stack.ps1. Tried: {'; '.join(tried)}")


async def _isolate(conn) -> None:
    """Make the pass deterministic inside this rolled-back transaction.

    Three pieces of shared state would otherwise decide the outcome, and
    all three come back on rollback:

      * `provider_health` holds admission for the WHOLE FLEET, so one
        stale open row in the dev database turns every test here into a
        false pass (`admit_pending` returns `[]` and means it).
      * every slot another goal holds counts against the CROSS-GOAL
        ceiling of 16.
      * every other PENDING row competes in the global pass, which is
        the one the fairness test has to use.

    The last is unparked by NULLING `goal_id`, not by moving the row to
    a terminal state: `admit_pending`'s query already excludes NULL-goal
    rows, and `goal_id` is not `state`, so NONE of the four state
    triggers on `executions` fire — no ledger rows, no NOTIFY, no slot
    release cascade. Moving them to CANCELLED would have fired all of
    them and made the fixture louder than the test.
    """
    await conn.execute("UPDATE provider_health SET state = 'closed' "
                       "WHERE state <> 'closed'")
    await conn.execute("UPDATE admission_slots SET released_at = now() "
                       "WHERE execution_id IS NOT NULL "
                       "AND released_at IS NULL")
    await conn.execute("UPDATE executions SET goal_id = NULL "
                       "WHERE state = 'PENDING' AND goal_id IS NOT NULL")


async def _goal(conn, ceiling: int = 4) -> str:
    goal = f"lt1-{uuid.uuid4().hex[:8]}"
    # RE-KEYED BY OWNER. Buckets were (goal, domain, kind); the
    # limit people wanted is per WHO -- the GO's errands, or one
    # person's assistant -- so the bucket is (goal, owner) and the
    # ceiling comes from the owner. A test ceiling is written onto
    # the row first, because `ensure_bucket` honours the row over
    # the default.
    await conn.execute(
        "INSERT INTO admission_buckets (goal_id, owner, domain, "
        "                               kind, ceiling) "
        "VALUES ($1, $2, 'any', 'any', $3) "
        "ON CONFLICT (goal_id, owner) DO UPDATE SET ceiling = $3",
        goal, OWNER, ceiling)
    await admit.ensure_bucket(conn, goal, OWNER)
    return goal


async def _pending(conn, goal: str, n: int = 1) -> list[int]:
    out = []
    for _ in range(n):
        out.append(await conn.fetchval(
            "INSERT INTO executions (kind, domain, state, goal_id) "
            "VALUES ('work', 'code', 'PENDING', $1) RETURNING id", goal))
    return out


def _run(body):
    async def main():
        conn = await _connect_or_skip()
        try:
            tx = conn.transaction()
            await tx.start()
            try:
                await _isolate(conn)
                await body(conn)
            finally:
                await tx.rollback()
        finally:
            await conn.close()
    asyncio.run(main())


# ── the contract: a waiting run with a free slot starts ────────────────

def test_a_pending_execution_with_a_free_slot_is_admitted():
    """The thing every invariant in section 7 is downstream of, and the
    thing nothing asserted until now."""
    async def body(conn):
        goal = await _goal(conn)
        [eid] = await _pending(conn, goal)

        assert await admit.admit_pending(conn, goal) == [eid]
        assert await conn.fetchval(
            "SELECT state FROM executions WHERE id = $1", eid) == "ADMITTED"
    _run(body)


def test_an_admitted_execution_holds_exactly_one_slot():
    """**THE REGRESSION.** The S4 fix claimed a second slot from a
    reserved `__system__` bucket to enforce the cross-goal ceiling.

        CREATE UNIQUE INDEX admission_slots_one_per_execution
            ON admission_slots (execution_id)
         WHERE execution_id IS NOT NULL AND released_at IS NULL

    Against a real schema the second INSERT raises out of `admit_pending`
    and the whole pass dies — for every goal, on every tick. Against the
    fakes it returned an id and looked fine. This assertion is the whole
    reason the file exists."""
    async def body(conn):
        goal = await _goal(conn)
        [eid] = await _pending(conn, goal)
        await admit.admit_pending(conn, goal)

        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots WHERE execution_id = $1 "
            "AND released_at IS NULL", eid) == 1
    _run(body)


def test_admission_survives_being_run_twice():
    """Level-triggered: the pass runs every tick and must be idempotent.
    A second pass over an already-ADMITTED row must not claim it a
    second slot either."""
    async def body(conn):
        goal = await _goal(conn)
        [eid] = await _pending(conn, goal)
        await admit.admit_pending(conn, goal)

        assert await admit.admit_pending(conn, goal) == []
        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots WHERE execution_id = $1 "
            "AND released_at IS NULL", eid) == 1
    _run(body)


# ── refusal is visible, which is the other half of the incident ────────

def test_a_full_bucket_refuses_and_writes_the_reason_on_the_row():
    """S1. The zero-slots incident stayed invisible for days because a
    full bucket skipped with a bare `continue`, and `reconcile` declines
    PENDING — so nothing anywhere aged the row out."""
    async def body(conn):
        goal = await _goal(conn, ceiling=1)
        first, second = await _pending(conn, goal, 2)

        assert await admit.admit_pending(conn, goal) == [first]
        row = await conn.fetchrow("SELECT state, stall_reason, stalled_at "
                                  "FROM executions WHERE id = $1", second)
        assert row["state"] == "PENDING"
        # THE REASON NAMES THE OWNER AND THE GOAL, which is what a
        # person can act on: finish one of theirs, or stop one. It used
        # to say "at its ceiling for coding/story" — the retired
        # (domain, kind) key, now always 'any'/'any', which would send
        # whoever read it looking for a limit that does not exist.
        reason = row["stall_reason"] or ""
        assert reason.startswith("admission:"), reason
        assert OWNER in reason, reason
        assert goal in reason, reason
        assert row["stalled_at"] is not None
    _run(body)


def test_admitting_clears_a_reason_left_from_an_earlier_refusal():
    """A stall_reason that outlives the wait is worse than none: the row
    moves on and the board still explains why it could not."""
    async def body(conn):
        goal = await _goal(conn, ceiling=1)
        first, second = await _pending(conn, goal, 2)
        await admit.admit_pending(conn, goal)          # second is refused

        await conn.execute("UPDATE admission_slots SET released_at = now() "
                           "WHERE execution_id = $1", first)
        assert second in await admit.admit_pending(conn, goal)
        row = await conn.fetchrow("SELECT stall_reason, stalled_at FROM "
                                  "executions WHERE id = $1", second)
        assert row["stall_reason"] is None and row["stalled_at"] is None
    _run(body)


# ── fairness, against real rows rather than a substring ────────────────

def test_a_busy_goal_does_not_starve_a_quiet_one():
    """S2. Both queue reads were a global `ORDER BY .. LIMIT 24`, and
    rows a full bucket would skip still consumed the limit — so one goal
    with 24+ PENDING starved every other goal, on every tick, for ever.

    The global pass (`goal_id=None`) is the shape that matters here."""
    async def body(conn):
        busy = await _goal(conn, ceiling=8)
        await _pending(conn, busy, 8)
        quiet = await _goal(conn)
        [wanted] = await _pending(conn, quiet)

        admitted = await admit.admit_pending(conn, None)
        assert wanted in admitted, "the quiet goal was starved"
        assert sum(1 for e in admitted if e != wanted) <= admit.goal_share()
    _run(body)


# ── and the repair for the incident itself ─────────────────────────────

def test_ensure_bucket_repairs_a_bucket_whose_slots_were_never_written():
    """The zero-slots incident, and it can no longer happen at all.

    `ensure_bucket` used to return early if the BUCKET existed, so a
    bucket with no slots was unrepairable: the early return fired every
    tick for ever and the goal admitted nothing while looking completely
    configured. Measured on `next` on 2026-09-16, eight of thirteen
    buckets were in that state.

    THE ASSERTION CHANGED WITH THE RE-KEYING, and in the right
    direction. `admit_pending` now ensures the owner's bucket inside the
    pass -- it has to, because owners appear as work does and a goal's
    second member has no bucket until their first run. So a goal whose
    slots vanished is repaired BY the pass rather than needing a
    separate repair call. The old test asserted the pass returned []
    first; asserting that now would be asserting the bug back.
    """
    async def body(conn):
        goal = await _goal(conn)
        await conn.execute(
            "DELETE FROM admission_slots WHERE goal_id = $1", goal)
        [eid] = await _pending(conn, goal)

        # One pass: notices the missing slots, writes them, admits.
        assert await admit.admit_pending(conn, goal) == [eid], (
            "a goal with no slots must be repaired by the pass itself")
        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots WHERE goal_id = $1",
            goal) > 0
    _run(body)


def test_the_repair_keeps_a_ceiling_somebody_widened():
    """The bucket's own ceiling is a decision that lives on the row; a
    caller passing the default must not silently narrow it."""
    async def body(conn):
        goal = await _goal(conn, ceiling=6)
        await admit.ensure_bucket(conn, goal, OWNER)   # owner default
        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots WHERE goal_id = $1",
            goal) == 6
    _run(body)
