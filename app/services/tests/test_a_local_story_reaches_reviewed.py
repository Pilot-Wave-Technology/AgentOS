"""A local story actually reaches `reviewed`.

IT DID NOT, AND SILENTLY. `record_review_for_story` wraps its write in
`async with conn.transaction()`; `asyncpg.Pool` has no `.transaction()` --
only a Connection does -- and the local lane passed the pool. AttributeError,
swallowed by the best-effort guard in `finish_local_story`, and the story sat
at `done` for ever: no `story_reviews` row, the thread still bound, the
workspace still on the protect list, and the review backstop unable to see it.

Confirmed on story 81ee97ea: result summary written, zero review rows, state
`done`. The pod lane never hit it because the worker passes a real connection.

These run against the real database because a pool/connection confusion is
invisible to anything that mocks the pool -- which is exactly what the
existing completion tests do.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


class _OneConnPool:
    """The thinnest thing with the Pool shape that matters here.

    `acquire()` as an async context manager, and NO `.transaction()` -- so a
    caller that reaches for `pool.transaction()` fails exactly as the real
    pool made it fail.
    """

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        @contextlib.asynccontextmanager
        async def _cm():
            yield conn
        return _cm()

    def __getattr__(self, name):
        if name == "transaction":
            raise AttributeError("'Pool' object has no attribute 'transaction'")
        return getattr(self._conn, name)


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _done_story(conn, sid: str):
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       f"{sid}-goal", "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,execution_mode,state) "
        "VALUES ($1,$2,$3,$4,'local','done')",
        sid, f"{sid}-goal", "review fixture", f"{PREFIX}-user")
    return f"{sid}-goal"


@pytest.mark.asyncio
async def test_a_finished_local_story_reaches_reviewed():
    from app.services.local_completion import mark_reviewed_as_local

    async with _conn() as conn:
        sid = f"{PREFIX}-rev-ok"
        goal = await _done_story(conn, sid)
        ok = await mark_reviewed_as_local(_OneConnPool(conn), sid, goal)
        assert ok is True, "the review write failed"
        state = await conn.fetchval("SELECT state FROM stories WHERE id=$1", sid)
        rows = await conn.fetch(
            "SELECT verdict, reviewer_agent_id FROM story_reviews WHERE story_id=$1", sid)
    assert state == "reviewed", (
        "the story stays at `done`: its thread stays bound and its workspace "
        "stays on the protect list"
    )
    assert len(rows) == 1
    assert rows[0]["verdict"] == "skipped_local"


@pytest.mark.asyncio
async def test_the_whole_finish_path_reports_what_it_did():
    """`finish_local_story` swallows each half independently, so a False is
    the only signal that the exemption did not get written.

    ON AN ERRAND, deliberately. A coding story now OWES a real review and must
    NOT take this exemption -- that ordering is pinned in
    `test_a_local_story_is_reviewed.py`. The exempt kinds are the ones that
    still finish in one step, and they are what this guards.
    """
    from app.services.local_completion import finish_local_story

    async with _conn() as conn:
        sid = f"{PREFIX}-rev-finish"
        goal = await _done_story(conn, sid)
        await conn.execute(
            "UPDATE stories SET work_kind = 'errand' WHERE id = $1", sid)
        out = await finish_local_story(_OneConnPool(conn), sid, goal)
    assert out["owes_review"] is False
    assert out["reviewed"] is True, (
        "finish_local_story reported success while the review was swallowed"
    )
