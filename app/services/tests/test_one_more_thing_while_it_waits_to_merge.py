"""The story waits with its tree up, and a person can still use it.

THIS IS THE FEATURE, not a convenience on top of it. Phase 1 made a
coding story hold its pod from push until merge; on its own that is
only a longer wait. `kernel_run_control.resume` un-parks a `direction`
hold and nothing else, so a run parked on a `landing` had no route back
into work at all -- the pod was held open for a door that did not exist.

WHAT THESE PIN. That asking for more work withdraws the approved plan
(the errand-282 failure), that it refuses when there is nothing to say,
that a merge landing mid-request wins, and that it is not quietly the
same act as `resume`.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

from app.services.kernel_run_control import ControlRefused
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


async def _parked_on_landing(conn):
    """A story waiting for its merge, with a thread the controls use."""
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid = f"{sid}-goal"
    tid = f"{sid}-thread"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO threads (id, goal_id, title, created_by) "
        "VALUES ($1,$2,$3,$4)", tid, gid, "story thread", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state,"
        "active_thread_id) VALUES ($1,$2,$3,'person','awaiting_merge',$4)",
        sid, gid, "waits for its merge", tid)
    # `_thread_story` reads `threads.story_id` -- the thread's own link
    # back -- not `stories.active_thread_id`. Both exist and only one is
    # the one the controls follow.
    await conn.execute("UPDATE threads SET story_id = $2 WHERE id = $1",
                       tid, sid)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, kind, domain, "
        "state, generation, convergence_generation) "
        "VALUES ($1,$2,'story','code','PARKED_HOT',7,1) RETURNING id",
        gid, sid)
    qid = await conn.fetchval(
        "INSERT INTO kernel.questions (execution_id, generation, kind, "
        "question_text, payload) VALUES ($1,7,'landing',$2,'{}'::jsonb) "
        "RETURNING id", eid, "waiting to land")
    return sid, gid, tid, eid, qid


class _Borrowed:
    """The test's own connection, which must survive being closed.

    `more_work` closes the connection it opened, in a `finally`. Here
    that connection belongs to the test and still holds the fixture rows
    in an open transaction, so closing it would take the rest of the
    assertions with it. asyncpg's `close` is read-only on the real
    object, hence a wrapper rather than a monkeypatch.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self):
        return None


@contextlib.contextmanager
def _conn_is(conn):
    """Point the module's kernel connection at the test's own.

    `_kernel_conn` would open a SECOND connection with its own
    transaction, which cannot see this test's uncommitted fixture rows
    and would block on them.
    """
    import app.services.kernel_more_work as m

    async def _fake():
        return _Borrowed(conn)
    orig = m._kernel_conn
    m._kernel_conn = _fake
    try:
        yield
    finally:
        m._kernel_conn = orig


@pytest.mark.asyncio
async def test_it_refuses_when_nothing_was_actually_asked_for():
    """"More work", unspecified, is the input that produces a confident
    second attempt at the wrong thing. The person is right here."""
    from app.services.kernel_more_work import more_work
    with pytest.raises(ControlRefused) as exc:
        await more_work("t", "g", "u", "   ")
    assert "say what else needs doing" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_asking_for_more_work_withdraws_the_approved_plan():
    """ERRAND 282. The plan gate is once per EXECUTION, not once per leg,
    so an approval in leg 1 leaves it open for the rest of the run. Asked
    for more work without a replan, the executor walked straight past the
    gate and measured new work against the old definition of done."""
    from app.services.kernel_more_work import more_work
    async with _conn() as conn:
        sid, gid, tid, eid, qid = await _parked_on_landing(conn)
        with _conn_is(conn):
            await more_work(tid, gid, "person-1", "also update the README")

        kinds = [r["kind"] for r in await conn.fetch(
            "SELECT kind FROM kernel.proposals WHERE execution_id = $1", eid)]
        assert "replan" in kinds, (
            "no replan was submitted — the executor would carry on under the "
            "plan that covered only the work already done")


@pytest.mark.asyncio
async def test_the_replan_tells_it_not_to_start_a_new_branch():
    """The branch is pushed and its PR is open. A second branch would
    orphan the first and leave the PR behind."""
    from app.services.kernel_more_work import more_work
    async with _conn() as conn:
        sid, gid, tid, eid, qid = await _parked_on_landing(conn)
        with _conn_is(conn):
            await more_work(tid, gid, "person-1", "also update the README")
        payload = await conn.fetchval(
            "SELECT payload::text FROM kernel.proposals "
            "WHERE execution_id = $1 AND kind = 'replan'", eid)
        assert "same branch" in payload
        assert "also update the README" in payload


@pytest.mark.asyncio
async def test_the_run_comes_back_to_life_and_the_story_with_it():
    from app.services.kernel_more_work import more_work
    async with _conn() as conn:
        sid, gid, tid, eid, qid = await _parked_on_landing(conn)
        with _conn_is(conn):
            await more_work(tid, gid, "person-1", "one more thing")

        run = await conn.fetchrow(
            "SELECT state, generation, convergence_generation "
            "FROM kernel.executions WHERE id = $1", eid)
        assert run["state"] == "RUNNING"
        assert run["generation"] == 8, "a resumed leg needs a new generation"
        assert run["convergence_generation"] == 8, (
            "the leg budget did not restart. A person answering is somebody "
            "watching; charging their decision to the convergence budget "
            "makes asking for more work self-limiting")
        assert await conn.fetchval(
            "SELECT status FROM kernel.questions WHERE id = $1", qid
        ) == "ANSWERED"


@pytest.mark.asyncio
async def test_a_merge_that_lands_first_wins():
    """The page can be seconds stale. If the merge answered the park
    between the render and the click, the work has landed and this needs
    a new story -- it must not drag a finished run back open."""
    from app.services.kernel_more_work import more_work
    async with _conn() as conn:
        sid, gid, tid, eid, qid = await _parked_on_landing(conn)
        await conn.execute(
            "UPDATE kernel.questions SET status = 'ANSWERED', "
            "answer_text = 'merged' WHERE id = $1", qid)
        with _conn_is(conn), pytest.raises(ControlRefused) as exc:
            await more_work(tid, gid, "person-1", "one more thing")
        assert "landed" in str(exc.value).lower()
        assert await conn.fetchval(
            "SELECT state FROM kernel.executions WHERE id = $1", eid
        ) == "PARKED_HOT", "the finished run was dragged back open"


@pytest.mark.asyncio
async def test_it_is_not_offered_when_the_story_is_not_waiting_to_merge():
    from app.services.kernel_more_work import available
    async with _conn() as conn:
        _, _, _, eid, qid = await _parked_on_landing(conn)
        assert await available(conn, eid) == qid
        await conn.execute(
            "UPDATE kernel.questions SET kind = 'acceptance' WHERE id = $1",
            qid)
        assert await available(conn, eid) is None, (
            "an acceptance park must not offer this route — answering the "
            "acceptance with what you want is how more work is asked for "
            "there, and two doors to one decision is how they drift")


def test_more_work_is_a_different_act_from_resume():
    """`resume` carries a STEER: it says HOW and leaves the definition of
    done alone. This changes what done means. One button for both would
    hide which one a person just performed."""
    import inspect

    from app.services import kernel_more_work, kernel_run_control
    assert "steer" in inspect.getsource(kernel_run_control.resume)
    assert "replan" in inspect.getsource(kernel_more_work.more_work)
    assert "steer" not in inspect.getsource(kernel_more_work.more_work)
