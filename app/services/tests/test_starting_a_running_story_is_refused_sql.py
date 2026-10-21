"""A story already running is refused in words, not by a stack trace.

`executions_one_live_per_story` is the right place for the rule — the
case it stops is two writers in the same moment, and there are two
conductor replicas. But a constraint refuses by RAISING, and that is a
bad answer for both callers that can reach it:

  * `start_story` is a tool. `UniqueViolationError` arrives at the
    assistant as a stack trace, and the only reading available to it is
    "the platform broke" — when what happened is ordinary.
  * the errand scheduler sweeps every tick. A run still going when the
    next tick fires is normal for any schedule whose period is shorter
    than its work; an exception per tick turns that into a recurring
    fault nobody can act on, and the sweep's own `except Exception`
    would record an `error` action each time.

So the read comes first and the constraint is the backstop. Both are
tested here, including the race the read cannot close.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "kernel,public"}),
                timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


async def _story(conn) -> tuple[str, str]:
    tag = uuid.uuid4().hex[:8]
    goal, story = f"R{tag}", f"s{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')",
        goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'t','accepted','alice')", story, goal)
    return goal, story


async def _exec(conn, goal: str, story: str, state: str) -> int:
    return await conn.fetchval(
        "INSERT INTO kernel.executions "
        "  (goal_id, story_id, state, generation, kind, domain) "
        "VALUES ($1,$2,$3,0,'story','coding') RETURNING id",
        goal, story, state)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "PARKED_HOT"])
async def test_a_live_run_is_found(state):
    from kernel.worker.one_live_run import live_run

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _story(conn)
        eid = await _exec(conn, goal, story, state)
        found = await live_run(conn, story)
        assert found is not None and found["id"] == eid
        assert found["state"] == state
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("done", ["DONE", "FAILED", "CANCELLED",
                                  "ABANDONED"])
async def test_a_finished_run_does_not_block_the_next_one(done):
    """A story that ended can be run again — forbidding that would make
    retrying anything impossible, which is the opposite of the point."""
    from kernel.worker.one_live_run import live_run

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _story(conn)
        await _exec(conn, goal, story, done)
        assert await live_run(conn, story) is None
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_story_less_execution_is_unconstrained():
    """Console fixtures and probes insert executions with no story and
    several coexisting is normal — the index has the same guard."""
    from kernel.worker.one_live_run import live_run

    conn = await _connect_or_skip()
    try:
        assert await live_run(conn, "") is None
        assert await live_run(conn, None) is None
    finally:
        await conn.close()


def test_the_message_names_the_run():
    """"Already running" without saying WHICH leaves the reader with no
    next step. With the id they can look at it, steer it or stop it."""
    from kernel.worker.one_live_run import already_running

    said = already_running("s1", {"id": 412, "state": "RUNNING"})
    assert "412" in said and "RUNNING" in said and "s1" in said


@pytest.mark.asyncio
async def test_the_constraint_still_catches_the_race():
    """THE BACKSTOP. Two starts in the same moment both read "clear".
    The read cannot close that, which is why the index exists — and why
    `start_story` catches the violation rather than letting it out."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _story(conn)
        await _exec(conn, goal, story, "RUNNING")
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await _exec(conn, goal, story, "PENDING")
    finally:
        await tx.rollback()
        await conn.close()


def test_start_story_refuses_before_it_inserts():
    """Read-then-insert, in that order. Inserting first and catching the
    violation would work, and would also burn a brief composition and an
    id sequence on every duplicate — but the real reason is the message:
    the refusal names the live run, and after a failed insert you have
    to go and look it up anyway."""
    import io
    import pathlib

    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "kernel/assistant/write.py", encoding="utf-8").read()
    body = src[src.index("async def start_story"):]
    body = body[:body.index("async def show_card")]
    assert body.index("live_run(conn, story)") < body.index(
        "INSERT INTO executions")
    assert "E_ALREADY_RUNNING" in body
    assert "UniqueViolationError" in body, (
        "the race the read cannot close is what the index is for; "
        "letting it out of the tool is the stack trace this avoids")


def test_the_scheduler_skips_rather_than_erroring():
    """A schedule whose period is shorter than its work comes round
    while the last run is in flight. That is the configuration working,
    not failing, and it must not read as an error every tick."""
    import io
    import pathlib

    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "kernel/worker/schedules.py", encoding="utf-8").read()
    assert "live_run" in src
    assert '"action": "skipped"' in src
