"""Concurrency budgets belong to WHO is running, per goal.

WHAT THE OLD KEY MEANT. `(goal_id, domain, kind)` with a ceiling of four
answered "how much coding work may this goal run at once" -- a question
nobody asked, wrong in both directions. A goal doing coding AND business
work held two independent budgets of four; a goal doing one kind was
capped at four however many people were waiting on it.

WHAT IS WANTED:

    the GO's autonomous errands    10 per goal
    each person's assistant         5 per goal
                                   20 across the whole system

PER GOAL, NOT PER PERSON, and that distinction has a test of its own:
somebody with an assistant in two goals gets five in each, because the
goal is part of the key.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid

import asyncpg
import pytest

from kernel.conductor.owner import (ASSISTANT_CEILING, CEILINGS, GO, UNKNOWN,
                                    ceiling_for)

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            # `admit.py` names these tables unqualified, so the
            # search_path is part of the contract -- the conductor
            # connects the same way.
            return await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "kernel,public"}),
                timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


def _goal() -> str:
    return "G" + uuid.uuid4().hex[:8]


# ── the numbers themselves ───────────────────────────────────────────

def test_the_go_gets_ten_and_an_assistant_five():
    assert CEILINGS[GO] == 10
    assert ASSISTANT_CEILING == 5
    assert ceiling_for(GO) == 10
    assert ceiling_for("assistant:anyone") == 5


def test_an_unattributable_run_is_limited_not_unlimited():
    """Unknown ownership must not mean no ceiling -- nor zero, which
    would wedge the run for ever."""
    assert ceiling_for(UNKNOWN) == ASSISTANT_CEILING
    assert UNKNOWN.startswith("assistant:")


def test_the_system_ceiling_is_twenty():
    from kernel.conductor.admit import SYSTEM_CEILING

    assert SYSTEM_CEILING == 20


def test_the_goal_share_can_reach_the_goal_ceiling():
    """THE SILENT ONE. `GOAL_SHARE` caps how many of a goal's rows enter
    the scan window per pass. At 4, a goal could never reach 10
    concurrent in a tick -- it would creep up four at a time and look
    mysteriously slow, with nothing anywhere saying why."""
    from kernel.conductor.admit import GOAL_SHARE

    assert GOAL_SHARE >= CEILINGS[GO], (
        f"a goal may run {CEILINGS[GO]} at once but only {GOAL_SHARE} of "
        f"its rows are scanned per pass")


# ── the buckets ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_bucket_is_made_with_the_owner_s_ceiling():
    from kernel.conductor.admit import ensure_bucket

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal = _goal()
        await ensure_bucket(conn, goal, GO)
        await ensure_bucket(conn, goal, "assistant:u1")

        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots "
            " WHERE goal_id = $1 AND owner = $2", goal, GO) == 10
        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots "
            " WHERE goal_id = $1 AND owner = $2", goal, "assistant:u1") == 5
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_same_assistant_in_two_goals_gets_two_budgets():
    """Five in each, not five between them. The goal is part of the key
    precisely so one goal's work cannot starve the same person's work
    somewhere else."""
    from kernel.conductor.admit import ensure_bucket

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        a, b, who = _goal(), _goal(), "assistant:same-person"
        await ensure_bucket(conn, a, who)
        await ensure_bucket(conn, b, who)
        for goal in (a, b):
            assert await conn.fetchval(
                "SELECT count(*) FROM admission_slots "
                " WHERE goal_id = $1 AND owner = $2", goal, who) == 5
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_domain_no_longer_splits_the_budget():
    """The old key gave a goal doing two kinds of work two budgets. One
    owner, one bucket, whatever the work is."""
    from kernel.conductor.admit import ensure_bucket

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, who = _goal(), "assistant:u2"
        await ensure_bucket(conn, goal, who)
        await ensure_bucket(conn, goal, who)          # idempotent
        assert await conn.fetchval(
            "SELECT count(*) FROM admission_buckets WHERE goal_id = $1",
            goal) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots WHERE goal_id = $1",
            goal) == 5
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_sixth_run_finds_no_slot():
    """The refusal IS zero rows -- the run stays PENDING and comes round
    again, rather than erroring."""
    from kernel.conductor.admit import _claim_slot, ensure_bucket

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, who = _goal(), "assistant:u3"
        await ensure_bucket(conn, goal, who)

        # REAL executions: `admission_slots.execution_id` is a foreign
        # key, so a claim against an invented id fails on the FK rather
        # than telling us anything about the ceiling.
        async def _mk() -> int:
            return await conn.fetchval(
                "INSERT INTO executions (goal_id, state, generation, "
                "                        kind, domain) "
                "VALUES ($1, 'PENDING', 0, 'story', 'coding') "
                "RETURNING id", goal)

        for _ in range(5):
            assert await _claim_slot(conn, await _mk(), goal, who) is not None
        assert await _claim_slot(conn, await _mk(), goal, who) is None, (
            "the sixth must find nothing free")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_hand_written_ceiling_is_not_undone():
    """Widening a goal by hand must survive the next tick -- the row's
    ceiling wins over the default."""
    from kernel.conductor.admit import ensure_bucket

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, who = _goal(), "assistant:u4"
        await conn.execute(
            "INSERT INTO admission_buckets (goal_id, owner, domain, kind, "
            "                               ceiling) "
            "VALUES ($1, $2, 'any', 'any', 7)", goal, who)
        await ensure_bucket(conn, goal, who)
        assert await conn.fetchval(
            "SELECT count(*) FROM admission_slots "
            " WHERE goal_id = $1 AND owner = $2", goal, who) == 7
    finally:
        await tx.rollback()
        await conn.close()


# ── attribution ──────────────────────────────────────────────────────

def test_the_errand_test_is_not_restated():
    """`kernel/go/drivable.py` already decides what an autonomous errand
    is -- work_kind plus a live grant. Two definitions would disagree
    the first time one was edited, and the disagreement would be a run
    counted against the wrong person's budget."""
    src = io.open("kernel/conductor/owner.py", encoding="utf-8").read()
    assert "errand_schedules" in src
    assert "revoked_at IS NULL" in src
    assert "work_kind" in src


def test_the_owner_is_stored_not_recomputed():
    """Ownership that changed mid-life would release a slot into a
    bucket it never claimed one from, and the counts would drift with
    nothing saying so."""
    src = io.open("kernel/conductor/owner.py", encoding="utf-8").read()
    assert "SELECT admission_owner FROM kernel.executions" in src
    assert "UPDATE kernel.executions SET admission_owner" in src
    assert "admission_owner IS NULL" in src, (
        "write it once; a later write would move a live run's budget")


def test_it_is_in_the_sequence():
    from app.services.tests.shipped_tree import (IS_DISTRIBUTION,
                                                 baseline_mentions,
                                                 table_has_column)

    if IS_DISTRIBUTION:
        # What the migration does, present in the shipped schema.
        assert table_has_column("kernel.executions", "admission_owner")
        assert table_has_column("kernel.admission_buckets", "owner")
        assert table_has_column("kernel.admission_slots", "owner")
        assert baseline_mentions(
            r"CREATE UNIQUE INDEX admission_buckets_by_owner ON "
            r"kernel\.admission_buckets USING btree \(goal_id, owner\)")
        return
    seq = io.open("migrations/SEQUENCE", encoding="utf-8").read()
    assert "admission_by_owner" in seq
