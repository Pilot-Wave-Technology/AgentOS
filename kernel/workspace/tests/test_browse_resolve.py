"""A story id that looks like a number is still a story id.

THE BUG. `resolve_execution` short-circuited on `str(story_id).isdigit()` —
"a bare integer IS an execution id", which the console's links rely on. But a
story id is EIGHT HEX CHARACTERS, so roughly one in forty-three of them
contains no letters at all. Story `52793688` was read as execution 52793688,
which does not exist, and the file explorer answered "this story has no
workspace yet" about a run that had a workspace and a mount.

Observed on KER20: the panel was missing for `52793688` and present for
`648f26a1`, on the same goal, at the same time. Intermittent by construction —
which is why it reads as flakiness rather than as a rule, and why it survived.

THE FIX IS ORDER, not a new format. Ask the story first; fall back to the
integer reading only when nothing names that story. The two id spaces cannot
collide in that direction: an execution id is a bigserial and no story is
created with one.

**How to run.** Needs the local stack's PostgreSQL with the kernel schema;
SKIPS without it. One transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from kernel.workspace.browse import resolve_execution

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
G = "zz-browse-goal"
# All digits, exactly the shape that broke.
NUMERIC_STORY = "52999311"
LETTERED_STORY = "6a8f26b1"


@contextlib.asynccontextmanager
async def _fixture():
    import os
    dsn = (os.environ.get("KERNEL_DATABASE_URL")
           or os.environ.get("DATABASE_URL") or LOCAL_DEV_DSN)
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn, server_settings={
                "search_path": "kernel,public"}), timeout=5)
    except Exception:                                       # noqa: BLE001
        pytest.skip("no local kernel database — start the local stack")
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,'zz-u')",
            G, "browse fixture")
        for sid in (NUMERIC_STORY, LETTERED_STORY):
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by) "
                "VALUES ($1,$2,$3,'zz-u')", sid, G, f"story {sid}")
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _run(conn, sid, container=None, state="RUNNING") -> int:
    """One run of `sid`. STATE IS A PARAMETER because a story may not
    have two live runs at once -- `executions_one_live_per_story`
    enforces it -- so a fixture that hardcoded RUNNING could not build
    the one scenario here that needs two."""
    return await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, state, goal_id, "
        " story_id, container_id) VALUES "
        "('story','coding',$4,$1,$2,$3) RETURNING id",
        G, sid, container, state)


def test_an_all_digits_story_id_resolves_to_its_run():
    """The KER20 failure, exactly."""
    async def run():
        async with _fixture() as conn:
            eid = await _run(conn, NUMERIC_STORY)
            got = await resolve_execution(conn, NUMERIC_STORY)
            assert got == eid, (
                f"resolved {got}, wanted run {eid} — an all-digit story id "
                f"was read as an execution id")
            assert got != int(NUMERIC_STORY)
    asyncio.run(run())


def test_a_lettered_story_id_still_resolves():
    """The case that always worked, and must keep working."""
    async def run():
        async with _fixture() as conn:
            eid = await _run(conn, LETTERED_STORY)
            assert await resolve_execution(conn, LETTERED_STORY) == eid
    asyncio.run(run())


def test_a_bare_execution_id_still_resolves():
    """The console addresses runs by number and its links must keep working —
    the reason the short-circuit existed. It survives as a FALLBACK."""
    async def run():
        async with _fixture() as conn:
            eid = await _run(conn, LETTERED_STORY)
            assert await resolve_execution(conn, str(eid)) == eid
    asyncio.run(run())


def test_the_live_run_wins_over_an_older_one():
    """A story amended after finishing has two runs. The files a person means
    are the ones being worked on.

    THE OLDER RUN IS DONE, and hardcoding it RUNNING is how this test
    came to assert something the database forbids: two live runs on one
    story violate `executions_one_live_per_story`, added 2026-09-24.
    The docstring said "after finishing" from the start -- the fixture
    just could not say it. Caught the first time the suite ran against a
    database carrying the constraint."""
    async def run():
        async with _fixture() as conn:
            await _run(conn, NUMERIC_STORY, state="DONE")    # no container
            live = await _run(conn, NUMERIC_STORY, container="abc123")
            assert await resolve_execution(conn, NUMERIC_STORY) == live
    asyncio.run(run())


def test_an_unknown_id_resolves_to_nothing():
    async def run():
        async with _fixture() as conn:
            assert await resolve_execution(conn, "zzzzzzzz") is None
            assert await resolve_execution(conn, "") is None
    asyncio.run(run())
