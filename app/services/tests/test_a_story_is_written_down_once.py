"""Changing a story changes both records, or it changes neither.

A story is written down twice — `stories.description` /
`.acceptance_criteria` for the board, and `executions.task` plus the
contract's checks for the executor — and until now nothing joined them.

THAT IS NOT A TIDINESS PROBLEM. The stored story text OUTRANKS
in-flight chat at every leg boundary, because a thin rehydration reads
the record rather than the conversation. Run 429 kept reverting a
correction for exactly this: the person said it, the executor heard it,
and the next leg read the unamended story and undid it. The only fix
that worked then was deleting the story and recreating it.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

from app.services.story_amend import AmendRefused
from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    await conn.execute("SET search_path = kernel, public")
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


class _Borrowed:
    """The test's connection, which must survive `amend_story` closing it."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self):
        return None


@contextlib.contextmanager
def _conn_is(conn):
    import app.services.story_amend as m
    import app.services.kernel_run_control as rc

    async def _fake():
        return _Borrowed(conn)
    orig = rc._kernel_conn
    rc._kernel_conn = _fake
    try:
        yield m
    finally:
        rc._kernel_conn = orig


async def _story_with_run(conn, *, state="in_progress", park=None,
                          exec_state="RUNNING"):
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid = f"{sid}-goal"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state,description) "
        "VALUES ($1,$2,$3,'person',$4,'the original')",
        sid, gid, "written down twice", state)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, kind, domain, "
        "state, generation, task, contract_id) "
        "VALUES ($1,$2,'story','code',$3,2,'the original task',NULL) "
        "RETURNING id", gid, sid, exec_state)
    # `amend` refuses a run with no contract AND no checks -- nothing
    # would define done for it -- so the fixture gives it one.
    cid = await conn.fetchval(
        "INSERT INTO kernel.contracts (goal_id, kind, domain, base_image) "
        "VALUES ($1,'story','code','scratch') RETURNING id", gid)
    await conn.execute(
        "UPDATE kernel.executions SET contract_id = $2 WHERE id = $1", eid, cid)
    qid = None
    if park:
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, generation, kind, "
            "question_text, payload) VALUES ($1,2,$2,'waiting','{}'::jsonb) "
            "RETURNING id", eid, park)
    return sid, gid, eid, qid


@pytest.mark.asyncio
async def test_both_records_move_together():
    async with _conn() as conn:
        sid, gid, eid, _ = await _story_with_run(conn)
        with _conn_is(conn) as m:
            await m.amend_story(gid, sid, "u1",
                                description="also handle the empty case")

        assert await conn.fetchval(
            "SELECT description FROM stories WHERE id = $1", sid
        ) == "also handle the empty case"
        task = await conn.fetchval(
            "SELECT task FROM kernel.executions WHERE id = $1", eid)
        assert "also handle the empty case" in task, (
            "the executor is still working to the old text — the next leg "
            "reads the record, not the conversation, and undoes the change")


@pytest.mark.asyncio
async def test_amending_withdraws_the_approved_plan():
    """A plan approved for the old scope is not approval for the new
    one. Leaving it in place measures new work against a contract
    nobody agreed to."""
    async with _conn() as conn:
        sid, gid, eid, _ = await _story_with_run(conn)
        with _conn_is(conn) as m:
            await m.amend_story(gid, sid, "u1", description="something else")
        kinds = [r["kind"] for r in await conn.fetch(
            "SELECT kind FROM kernel.proposals WHERE execution_id = $1", eid)]
        assert "replan" in kinds


@pytest.mark.asyncio
async def test_a_story_with_no_run_still_updates_its_own_record():
    """Not every story is in flight. A proposed one has no execution and
    must still be editable."""
    async with _conn() as conn:
        sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
        gid = f"{sid}-goal"
        await conn.execute(
            "INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
            gid, "fixture", f"{PREFIX}-user")
        await conn.execute(
            "INSERT INTO stories (id,goal_id,title,created_by,state) "
            "VALUES ($1,$2,'no run','person','proposed')", sid, gid)
        with _conn_is(conn) as m:
            got = await m.amend_story(gid, sid, "u1", description="new text")
        assert got["execution_id"] is None
        assert await conn.fetchval(
            "SELECT description FROM stories WHERE id = $1", sid) == "new text"


@pytest.mark.asyncio
async def test_amending_a_waiting_story_retires_the_question_it_answers():
    """A run parked on a `closeout` is being asked whether the story is
    finished. Amending it answers that: no. Left open, it asks a person
    to rule on a story that no longer exists in that form — and
    `questions_one_open` would refuse the next park behind it."""
    async with _conn() as conn:
        sid, gid, eid, qid = await _story_with_run(
            conn, state="awaiting_merge", park="closeout",
            exec_state="PARKED_HOT")
        with _conn_is(conn) as m:
            await m.amend_story(gid, sid, "u1", description="one more thing")
        q = await conn.fetchrow(
            "SELECT status, verdict FROM kernel.questions WHERE id = $1", qid)
        assert q["status"] == "ANSWERED"
        assert q["verdict"] == "revise", (
            "the approach was not rejected; the job changed")


@pytest.mark.asyncio
async def test_the_plan_is_withdrawn_exactly_once():
    """`amend` submits a replan, and `resolve_request`'s arms for these
    parks submit their own. Two withdrawals of one approval reach the
    model as two separate instructions to re-plan."""
    async with _conn() as conn:
        sid, gid, eid, _ = await _story_with_run(
            conn, state="awaiting_merge", park="closeout",
            exec_state="PARKED_HOT")
        with _conn_is(conn) as m:
            await m.amend_story(gid, sid, "u1", description="one more thing")
        assert await conn.fetchval(
            "SELECT count(*) FROM kernel.proposals "
            " WHERE execution_id = $1 AND kind = 'replan'", eid) == 1


@pytest.mark.asyncio
async def test_nothing_moves_when_the_run_refuses():
    """HALF AN AMENDMENT IS WORSE THAN NONE: two records that disagree,
    and the one that wins is the one nobody edited."""
    async with _conn() as conn:
        sid, gid, eid, _ = await _story_with_run(conn)
        # A LIVE run that `amend` itself refuses: no contract and no
        # checks means nothing defines done for it, so the adjudicator
        # could not judge the amended work. A FAILED run would not do —
        # it is not the live one, so the story-only path would take over
        # and succeed, proving nothing.
        await conn.execute(
            "UPDATE kernel.executions SET contract_id = NULL WHERE id = $1",
            eid)
        with _conn_is(conn) as m, pytest.raises(AmendRefused) as exc:
            await m.amend_story(gid, sid, "u1", description="never lands")
        assert "NOT changed" in str(exc.value), (
            "the refusal must say the story record was left alone, or a "
            "person retries an edit that half-happened")
        assert await conn.fetchval(
            "SELECT description FROM stories WHERE id = $1", sid
        ) == "the original", (
            "the story row moved while the run did not — exactly the "
            "disagreement this module exists to prevent")


@pytest.mark.asyncio
async def test_a_merge_story_is_not_editable():
    async with _conn() as conn:
        sid, gid, _, _ = await _story_with_run(conn)
        await conn.execute(
            "UPDATE stories SET story_kind = 'merge' WHERE id = $1", sid)
        with _conn_is(conn) as m, pytest.raises(AmendRefused) as exc:
            await m.amend_story(gid, sid, "u1", description="x")
        assert "Git Engine" in str(exc.value)


@pytest.mark.asyncio
async def test_an_amendment_must_change_something():
    async with _conn() as conn:
        sid, gid, _, _ = await _story_with_run(conn)
        with _conn_is(conn) as m, pytest.raises(AmendRefused):
            await m.amend_story(gid, sid, "u1")
