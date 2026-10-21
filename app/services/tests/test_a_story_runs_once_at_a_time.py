"""One story, one live execution, enforced by the database.

WHY NOT A CHECK IN THE DISPATCHER. The case that has to be stopped is
two writers in the same moment -- a re-dispatch racing a queued row, a
retry that did not see the first attempt, a reconcile firing twice --
and that is exactly when a read-then-write check passes twice. There are
two conductor replicas.

WHAT IT COSTS WITHOUT THE GUARD, and it is not an error anybody sees:
both rows queue, both claim an admission slot, and both run the same
story in separate pods against the same branch. At a system ceiling of
20 that is a tenth of the fleet racing itself, and the visible symptom
is two sets of commits and two acceptance cards.

THE TERMINAL SET HAS TO AGREE WITH THE POD LOOP'S. An index predicate
cannot import Python, so the four states are restated in SQL -- which
means a new state has to be classified in both places. The last test
here is what notices if they drift.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid

import asyncpg
import pytest

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
TERMINAL = ("DONE", "FAILED", "CANCELLED", "ABANDONED")


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


async def _exec(conn, story_id: str, state: str) -> int:
    return await conn.fetchval(
        "INSERT INTO kernel.executions "
        "  (goal_id, story_id, state, generation, kind, domain) "
        "VALUES ('G1', $1, $2, 0, 'story', 'coding') RETURNING id",
        story_id, state)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "PARKED_HOT",
                                   "VERIFYING"])
async def test_a_second_live_execution_is_refused(state):
    """THE RACE. The second INSERT fails rather than queueing."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        story = uuid.uuid4().hex[:8]
        await _exec(conn, story, state)
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await _exec(conn, story, "PENDING")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("done", TERMINAL)
async def test_a_finished_story_may_run_again(done):
    """A story that ended can be re-run -- a new execution after a
    terminal one is ordinary, and forbidding it would make retrying
    anything impossible."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        story = uuid.uuid4().hex[:8]
        await _exec(conn, story, done)
        assert await _exec(conn, story, "PENDING"), (
            f"a new execution after {done} must be allowed")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_many_finished_executions_may_coexist():
    """The index is partial. Without the predicate, history itself would
    violate it -- a story retried three times has three terminal rows."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        story = uuid.uuid4().hex[:8]
        for st in ("DONE", "FAILED", "CANCELLED"):
            await _exec(conn, story, st)
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_executions_without_a_story_are_untouched():
    """Console fixtures and probes insert executions with no story, and
    several of them coexisting is normal."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        for _ in range(3):
            await conn.execute(
                "INSERT INTO kernel.executions "
                "  (goal_id, story_id, state, generation, kind, domain) "
                "VALUES ('G1', NULL, 'PENDING', 0, 'story', 'coding')")
    finally:
        await tx.rollback()
        await conn.close()


def test_the_terminal_set_matches_the_pod_loop():
    """The index predicate restates four states that Python holds in
    `pod_loop.TERMINAL`. An index cannot import, so the two are kept in
    step by this -- a state added to one and not the other either blocks
    a legitimate re-run or lets a duplicate through."""
    from kernel.worker.pod_loop import TERMINAL as LOOP_TERMINAL
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, baseline_schema

    if IS_DISTRIBUTION:
        # The squashed baseline carries the same predicate, as pg_dump
        # writes it: each state cast to ::text.
        sql = baseline_schema().split(
            "CREATE UNIQUE INDEX executions_one_live_per_story")[1]
    else:
        sql = io.open("migrations/one_live_execution_per_story.sql",
                      encoding="utf-8").read()
    predicate = sql.split("state <> ALL (ARRAY[")[1].split("]")[0]
    in_sql = {w.strip().replace("::text", "").strip("'")
              for w in predicate.split(",")}
    assert in_sql == set(LOOP_TERMINAL), (
        f"the index predicate says {sorted(in_sql)} and the pod loop says "
        f"{sorted(LOOP_TERMINAL)} -- classify the new state in both")


def test_it_is_in_the_sequence():
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, baseline_mentions

    if IS_DISTRIBUTION:
        # No SEQUENCE entry to find; the index itself must be in the schema.
        assert baseline_mentions(
            r"CREATE UNIQUE INDEX executions_one_live_per_story "
            r"ON kernel\.executions USING btree \(story_id\)"), (
            "the one-live-execution-per-story index is not in the baseline")
    else:
        seq = io.open("migrations/SEQUENCE", encoding="utf-8").read()
        assert "one_live_execution_per_story" in seq
