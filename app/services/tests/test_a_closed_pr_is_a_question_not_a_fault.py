"""Somebody closed the PR without merging. That is an act, not a fault.

GitHub distinguishes closed from merged, so nothing broke and nothing
needs diagnosing — a person decided something and only they can say
what. Guessing is expensive in both directions: cancel a story they
meant to keep, or hold a pod for one they abandoned. Under the new
lifecycle the second is the live risk, because the run is parked on a
`landing` waiting for a merge that is never coming, and the deadline
janitor would be the only thing that ever noticed, three days later.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


class _Borrowed:
    """The test's own connection, surviving the code closing it.

    The sweeps now open a KERNEL connection (`search_path=kernel,public`)
    rather than taking one from the pool, because the kernel helpers they
    call write unqualified `questions` / `executions`. In a test that
    connection must be the one holding the fixture's open transaction, and
    it must not actually close.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self):
        return None


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    await conn.execute("SET search_path = kernel, public")
    tx = conn.transaction()
    await tx.start()
    import app.services.story_landing as _sl
    _orig = _sl._kernel_conn

    async def _borrowed():
        return _Borrowed(conn)
    _sl._kernel_conn = _borrowed
    try:
        yield conn
    finally:
        _sl._kernel_conn = _orig
        await tx.rollback()
        await conn.close()


class _PoolOnConn:
    def __init__(self, conn):
        self._conn = conn

    async def fetch(self, *a):
        return await self._conn.fetch(*a)

    async def fetchval(self, *a):
        return await self._conn.fetchval(*a)

    async def execute(self, *a):
        return await self._conn.execute(*a)

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _CM()


async def _waiting_story(conn, *, node_state="ESCALATED",
                         error="PR closed unmerged (human-cancel)"):
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid, rid = f"{sid}-goal", uuid.uuid4().hex
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state) "
        "VALUES ($1,$2,$3,'person','awaiting_merge')",
        sid, gid, "its PR was closed")
    await conn.execute(
        "INSERT INTO git_repos (id, owner, name) VALUES ($1,$2,$3)",
        rid, "acme", f"repo-{rid[:6]}")
    nid = uuid.uuid4().hex
    await conn.execute(
        "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state, "
        "last_error, pr_number) VALUES ($1,$2,$3,$4,$5,$6,42)",
        nid, rid, gid, f"feat/{sid}", node_state, error)
    await conn.execute(
        "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
        "VALUES ($1,$2,1)", nid, sid)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, kind, domain, "
        "state, generation) VALUES ($1,$2,'story','code','PARKED_HOT',5) "
        "RETURNING id", gid, sid)
    qid = await conn.fetchval(
        "INSERT INTO kernel.questions (execution_id, generation, kind, "
        "question_text, payload) VALUES ($1,5,'landing',$2,'{}'::jsonb) "
        "RETURNING id", eid, "waiting to land")
    return sid, gid, eid, qid, nid


def test_only_a_person_can_say_what_closing_a_pr_meant():
    from kernel.mailbox.schema import needs_a_person
    assert needs_a_person("pr_closed") is True
    assert needs_a_person("pr_closed", go_driven=True) is True


def test_the_card_offers_carrying_on_or_cancelling_and_not_approval():
    """"Approve" and "decline" are category errors here: there is
    nothing to approve, and refusing is not what cancelling means."""
    from app.services.kernel_park_cards import build_card
    card = build_card({"kind": "pr_closed", "id": 1, "execution_id": 1,
                       "goal_id": "g", "story_id": "s",
                       "question_text": "now what?"}, None, "")
    labels = [o["label"] for o in card["options"]]
    assert any("Keep working" in lbl for lbl in labels)
    assert any("Cancel the story" in lbl for lbl in labels)
    assert not any("Approve" in lbl for lbl in labels)
    assert card["needs_human"] is True


@pytest.mark.asyncio
async def test_the_dead_wait_is_closed_and_the_question_asked(monkeypatch):
    """`questions_one_open` allows exactly one, so the landing park has
    to be retired before anything answerable can be asked. Left open, it
    waits for a merge that is not coming until the 72h deadline."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        _, _, eid, qid, _ = await _waiting_story(conn)
        assert await story_landing.sweep_pr_closed(_PoolOnConn(conn)) == 1

        assert await conn.fetchval(
            "SELECT status FROM kernel.questions WHERE id = $1", qid
        ) == "ANSWERED"
        asked = await conn.fetchrow(
            "SELECT status, payload FROM kernel.questions "
            " WHERE execution_id = $1 AND kind = 'pr_closed'", eid)
        assert asked is not None and asked["status"] == "OPEN"
        assert await conn.fetchval(
            "SELECT state FROM kernel.executions WHERE id = $1", eid
        ) == "PARKED_HOT", "the pod was released before they answered"


@pytest.mark.asyncio
async def test_asking_twice_asks_once(monkeypatch):
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        _, _, eid, _, _ = await _waiting_story(conn)
        assert await story_landing.sweep_pr_closed(_PoolOnConn(conn)) == 1
        assert await story_landing.sweep_pr_closed(_PoolOnConn(conn)) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM kernel.questions WHERE execution_id = $1 "
            "AND kind = 'pr_closed'", eid) == 1


@pytest.mark.asyncio
async def test_a_multi_repo_story_still_landing_elsewhere_is_not_asked(
        monkeypatch):
    """One repo's PR closed while another is still open is not a story
    that has stopped. Offering to cancel it would offer to throw away
    work that is still on its way to trunk."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        sid, gid, _, _, _ = await _waiting_story(conn)
        rid2 = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name) VALUES ($1,$2,$3)",
            rid2, "acme", f"repo-{rid2[:6]}")
        nid2 = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, "
            "state) VALUES ($1,$2,$3,$4,'BRANCH_CREATED')",
            nid2, rid2, gid, f"feat/{sid}-two")
        await conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
            "VALUES ($1,$2,2)", nid2, sid)
        assert await story_landing.sweep_pr_closed(_PoolOnConn(conn)) == 0


@pytest.mark.asyncio
async def test_a_node_escalated_for_some_other_reason_is_not_this(
        monkeypatch):
    """ESCALATED covers transient faults too — a 403 burst, a flaky
    required check. Those are not a person closing a PR, and asking
    "cancel the story?" about one would be alarming and wrong."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        await _waiting_story(conn, error="required check 'ci' failed twice")
        assert await story_landing.sweep_pr_closed(_PoolOnConn(conn)) == 0


@pytest.mark.asyncio
async def test_the_flag_off_changes_nothing():
    from app.services import story_landing
    async with _conn() as conn:
        await _waiting_story(conn)
        assert await story_landing.sweep_pr_closed(_PoolOnConn(conn)) == 0


def test_cancelling_goes_through_the_mailbox_not_a_direct_write():
    """`apply_cancellation` is guarded on RUNNING and this run is
    PARKED_HOT until the un-park at the END of `resolve_request` — so a
    direct call would be guarded out and the un-park would then set the
    story running again, which is the opposite of what was asked."""
    import inspect

    from kernel.harness import park_resolve
    src = inspect.getsource(park_resolve)
    i = src.index('q_kind == "pr_closed" and settled != "approve"')
    arm = src[i:i + 1800]
    assert '"cancel"' in arm, "the cancel is not submitted as a proposal"
    assert "apply_cancellation(" not in arm, (
        "cancelling a PARKED_HOT run by direct call is guarded out, and "
        "the un-park below would then resume the story")


def test_keeping_going_withdraws_the_plan():
    """Whatever happens next is a new cycle, and the old approval
    covered the work that was just rejected."""
    import inspect

    from kernel.harness import park_resolve
    src = inspect.getsource(park_resolve)
    i = src.index('elif q_kind == "pr_closed":')
    arm = src[i:i + 1800]
    assert '"replan"' in arm
    assert "do NOT start over" in arm, (
        "nothing tells it the work is still on the branch; an executor "
        "that reads a closed PR as rejection rebuilds from scratch")
