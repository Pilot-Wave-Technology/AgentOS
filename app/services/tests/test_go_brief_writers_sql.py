"""The GO brief/decision writers, CALLED — not re-implemented in the test.

WHY THIS EXISTS. `update_brief` shipped with a query that declared `$2` and
passed one argument:

    "UPDATE goals SET brief = $2, brief_updated = now() "
    " WHERE id = $1 RETURNING brief_updated", goal_id

Every call raised `InterfaceError: the server expects 2 arguments for this
query, 1 was passed`. The surface caught it and returned the refusal correctly —
`{"refused": "E_TOOL_FAILED", ...}` — and GO reported to the person that it had
written the brief. Two full goals ran with an empty brief and nobody could see
why, because the only place the truth appeared was a stack trace in a log.

THE GAP THAT LET IT SHIP is the point of this file. Stage L had tests, and they
were green: `test_stage_l_knowledge` exercises `_merge`, `normalise`, `address`,
`live`, `fingerprint` — pure functions, no database. `test_stage_l_grants_sql`
exercises the GRANTS, with SQL written by hand in the test. So the logic was
proven and the permission was proven and the actual writer function was never
once called. A parameter-count bug is invisible to both.

So these call the real functions against a real database. Nothing here restates
a query — if a writer's SQL and its arguments disagree, the call raises, and
that is the whole assertion.

**How to run.** Needs the local stack's PostgreSQL; SKIPS without it. One
transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

asyncpg = pytest.importorskip("asyncpg")

G = "zz-brief-goal"


@contextlib.asynccontextmanager
async def _goal():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
            G, "brief fixture", "zz-brief-user")
        yield conn
    finally:
        await tx.rollback()
        left = await conn.fetchval(
            "SELECT count(*) FROM goals WHERE id = $1", G)
        await conn.close()
        assert left == 0, "fixture survived the rollback — dev DB dirtied"


def test_update_brief_actually_stores_the_text():
    """The bug, exactly: the text must reach the column."""
    async def run():
        async with _goal() as conn:
            from kernel.go.goal_brief import update_brief

            out = await update_brief(conn, G, "go:test", "where things stand")
            assert not getattr(out, "code", None), out   # not a Refused

            row = await conn.fetchrow(
                "SELECT brief, brief_updated FROM goals WHERE id=$1", G)
            assert row["brief"] == "where things stand", row["brief"]
            assert row["brief_updated"] is not None
    asyncio.run(run())


def test_update_brief_replaces_rather_than_appends():
    """It is THE current picture, not a log of pictures."""
    async def run():
        async with _goal() as conn:
            from kernel.go.goal_brief import update_brief

            await update_brief(conn, G, "go:test", "first")
            await update_brief(conn, G, "go:test", "second")
            assert await conn.fetchval(
                "SELECT brief FROM goals WHERE id=$1", G) == "second"
    asyncio.run(run())


def test_read_brief_round_trips_what_update_wrote():
    """The pair must agree; a writer nobody can read back is not a feature."""
    async def run():
        async with _goal() as conn:
            from kernel.go.goal_brief import read_brief, update_brief

            assert (await read_brief(conn, G))["text"] == ""
            await update_brief(conn, G, "go:test", "the picture")
            got = await read_brief(conn, G)
            assert got["text"] == "the picture"
            assert got["updated"] is not None
    asyncio.run(run())


def test_an_empty_brief_is_refused_not_written():
    """Clearing it leaves a reader with the board and no reading of it."""
    async def run():
        async with _goal() as conn:
            from kernel.go.goal_brief import update_brief

            await update_brief(conn, G, "go:test", "real content")
            out = await update_brief(conn, G, "go:test", "   ")
            assert getattr(out, "code", "") == "E_EMPTY_BRIEF", out
            assert await conn.fetchval(
                "SELECT brief FROM goals WHERE id=$1", G) == "real content"
    asyncio.run(run())


def test_retire_decision_marks_it_and_keeps_the_row():
    """Retired is REMOVED for readers and still present for `why did we stop`."""
    async def run():
        async with _goal() as conn:
            from kernel.go.goal_brief import retire_decision

            await conn.execute(
                "INSERT INTO goal_decisions (id, goal_id, decision, rationale) "
                "VALUES ('zz-d1',$1,'use X','because')", G)

            out = await retire_decision(conn, G, "go:test", "zz-d1",
                                        "X was replaced")
            assert not getattr(out, "code", None), out

            row = await conn.fetchrow(
                "SELECT retired_at, rationale FROM goal_decisions "
                "WHERE id='zz-d1'")
            assert row["retired_at"] is not None
            assert "X was replaced" in row["rationale"]

            # Retiring it twice is refused, not a silent second write.
            again = await retire_decision(conn, G, "go:test", "zz-d1", "")
            assert getattr(again, "code", "") == "E_NO_SUCH_DECISION", again
    asyncio.run(run())
