"""The run holds its pod from push until merge, and then finishes.

WHAT THIS IS TESTING, in one sentence: accepting a coding story used to
end it, and now it only ends the WORK -- the run parks, the story waits
at `awaiting_merge`, and the merge is what finishes both.

Written against the real database rather than fakes because every
interesting property here is a predicate: "has anything NOT merged yet",
"is this run still parked", "did the story move". A fake would let all
four of those pass while the SQL was wrong, which is the only way they
realistically go wrong.
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
    """A kernel-shaped connection.

    `search_path = kernel, public` is not a detail: every one of these
    modules writes `executions` and `questions` unqualified, because the
    kernel's own pool is built that way (`kernel/assistant/asgi.py`). A
    test connection on the default path resolves none of them, and the
    failure -- `relation "executions" does not exist` -- looks like a
    missing migration rather than a missing setting.
    """
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


@contextlib.contextmanager
def _flag_on(monkeypatch):
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    yield


async def _coding_story(conn, *, node_state: str | None = "BRANCH_CREATED",
                        kind: str = "story", created_by: str = "person"):
    """A story, a run, and optionally a git node riding it."""
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid = f"{sid}-goal"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state) "
        "VALUES ($1,$2,$3,$4,'in_progress')",
        sid, gid, "waits for its merge", created_by)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, kind, domain, "
        "state, generation) VALUES ($1,$2,$3,'code','PARKED_HOT',3) "
        "RETURNING id", gid, sid, kind)
    if node_state is not None:
        rid = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name) VALUES ($1,$2,$3)",
            rid, "acme", f"repo-{rid[:6]}")
        nid = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, "
            "state) VALUES ($1,$2,$3,$4,$5)",
            nid, rid, gid, f"feat/{sid}", node_state)
        await conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
            "VALUES ($1,$2,1)", nid, sid)
        return sid, gid, eid, nid
    return sid, gid, eid, None


# ── does it owe a landing ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_coding_story_with_an_unmerged_branch_owes_a_landing(monkeypatch):
    from kernel.worker.landing import owes_a_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, _ = await _coding_story(conn)
            assert await owes_a_landing(conn, eid) is True


@pytest.mark.asyncio
async def test_nothing_left_unmerged_means_nothing_to_wait_for(monkeypatch):
    """THE CASE THAT WOULD DEADLOCK. A story whose branch landed while it
    was being verified must finish now -- parking it would be a wait for
    a merge that already happened, and nothing would ever answer it."""
    from kernel.worker.landing import owes_a_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, _ = await _coding_story(conn, node_state="MERGED")
            assert await owes_a_landing(conn, eid) is False


@pytest.mark.asyncio
async def test_a_story_with_no_branch_at_all_owes_nothing(monkeypatch):
    from kernel.worker.landing import owes_a_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, _ = await _coding_story(conn, node_state=None)
            assert await owes_a_landing(conn, eid) is False


@pytest.mark.asyncio
async def test_an_errand_never_owes_a_landing(monkeypatch):
    """Read from the RUN's kind, not the story row's `work_kind`: the
    run's kind is what resolved the profile that actually ran."""
    from kernel.worker.landing import owes_a_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, _ = await _coding_story(conn, kind="errand")
            assert await owes_a_landing(conn, eid) is False


@pytest.mark.asyncio
async def test_the_engines_own_plumbing_never_owes_a_landing(monkeypatch):
    """A merge story IS the landing. Parking one to wait for itself would
    deadlock the lane it exists to drain."""
    from kernel.worker.landing import owes_a_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, _ = await _coding_story(conn, created_by="engine")
            assert await owes_a_landing(conn, eid) is False


@pytest.mark.asyncio
async def test_the_flag_off_changes_nothing():
    """Ships dark. Off, every path writes exactly what it wrote before."""
    from kernel.worker.landing import owes_a_landing
    async with _conn() as conn:
        _, _, eid, _ = await _coding_story(conn)
        assert await owes_a_landing(conn, eid) is False


# ── the park, and what ends it ───────────────────────────────────────

@pytest.mark.asyncio
async def test_parking_names_the_branches_it_waits_on(monkeypatch):
    from kernel.worker.landing import park_for_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, _, eid, _ = await _coding_story(conn)
            assert await park_for_landing(conn, eid) is True
            q = await conn.fetchrow(
                "SELECT kind, status, payload FROM kernel.questions "
                "WHERE execution_id = $1", eid)
            assert q["kind"] == "landing"
            assert q["status"] == "OPEN"
            import json
            assert json.loads(q["payload"])["branches"] == [f"feat/{sid}"]


@pytest.mark.asyncio
async def test_the_merge_card_is_a_persons_card_but_not_for_the_merge(
        monkeypatch):
    """IT ASKS FOR THE OTHER ANSWER, and that is why it has controls.

    The merge itself is recorded without anybody touching the card --
    `story_landing` picks it up from the forge, and a button saying "I
    merged it" would be a second authority over one fact, free to
    disagree with the first. What the card carries is the OTHER answer:
    a person who reads the PR and wants changes instead. Without a
    control they would have a story they can neither merge nor redirect.
    """
    from app.services.kernel_park_cards import build_card
    from kernel.mailbox.schema import needs_a_person
    assert needs_a_person("landing") is True

    card = build_card({"kind": "landing", "id": 1, "execution_id": 1,
                       "goal_id": "g", "story_id": "s",
                       "question_text": "merge?"}, None, "")
    values = [o["value"] for o in card["options"]]
    assert values == ["revise"], (
        "the merge card offers something other than 'more work' — the "
        "merge is answered on the forge, not here")
    assert card["allow_custom"] is True


@pytest.mark.asyncio
async def test_parking_twice_does_not_open_a_second_park(monkeypatch):
    """`questions_one_open` would refuse it anyway; this proves the
    refusal is handled rather than raised at the caller."""
    from kernel.worker.landing import park_for_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, _ = await _coding_story(conn)
            assert await park_for_landing(conn, eid) is True
            n = await conn.fetchval(
                "SELECT count(*) FROM kernel.questions WHERE execution_id = $1",
                eid)
            assert n == 1


# ── the merge is the answer ──────────────────────────────────────────

class _PoolOnConn:
    """One connection wearing a pool's interface.

    The sweeps take a pool because they run from the web tier; the test
    holds a single connection inside a rolled-back transaction. asyncpg
    nests a transaction as a SAVEPOINT, so the inner `conn.transaction()`
    these open commits into the outer one and the rollback still cleans
    up everything.
    """

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


@pytest.mark.asyncio
async def test_the_story_follows_the_run_into_awaiting_merge(monkeypatch):
    """The run parks; the sweep moves the story. The story row is a
    projection of the execution, not the other way round."""
    from app.services import story_landing
    from kernel.worker.landing import park_for_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, _, eid, _ = await _coding_story(conn)
            await park_for_landing(conn, eid)
            assert await story_landing.sweep_entering(_PoolOnConn(conn)) == 1
            assert await conn.fetchval(
                "SELECT state FROM stories WHERE id = $1", sid
            ) == "awaiting_merge"


@pytest.mark.asyncio
async def test_a_still_unmerged_story_is_not_released(monkeypatch):
    """The whole point. While anything is unmerged the run keeps its pod."""
    from app.services import story_landing
    from kernel.worker.landing import park_for_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, _ = await _coding_story(conn)
            await park_for_landing(conn, eid)
            assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 0
            assert await conn.fetchval(
                "SELECT status FROM kernel.questions WHERE execution_id = $1",
                eid) == "OPEN"


@pytest.mark.asyncio
async def test_the_merge_answers_the_park_and_asks_a_person_instead(monkeypatch):
    """The merge ends the WAIT, not the story.

    This asserted the run went straight to DONE until card [B] landed.
    It does not any more, and that is the feature: the work is on trunk
    and the workspace is still up, which is the one moment "actually,
    one more thing" can continue THIS story. So the run parks again, on
    a `closeout`, and a person has the last word.
    """
    from app.services import story_landing
    from kernel.worker.landing import park_for_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            _, _, eid, nid = await _coding_story(conn)
            await park_for_landing(conn, eid)
            await conn.execute(
                "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)

            assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 1
            landing = await conn.fetchrow(
                "SELECT status, answered_by FROM kernel.questions "
                "WHERE execution_id = $1 AND kind = 'landing'", eid)
            assert landing["status"] == "ANSWERED"
            assert landing["answered_by"] == "git-engine", (
                "a person must not be recorded as having answered a park "
                "nobody was asked")

            closeout = await conn.fetchrow(
                "SELECT status FROM kernel.questions "
                "WHERE execution_id = $1 AND kind = 'closeout'", eid)
            assert closeout is not None and closeout["status"] == "OPEN", (
                "the merge landed and nobody was asked whether the story "
                "is finished — the workspace would be released with the "
                "last chance to add to this story unused")
            assert await conn.fetchval(
                "SELECT state FROM executions WHERE id = $1", eid
            ) == "PARKED_HOT", (
                "the run finished at the merge; it must hold its pod until "
                "a person closes the story")


@pytest.mark.asyncio
async def test_a_multi_repo_story_waits_for_the_LAST_branch(monkeypatch):
    """The engine knowing ONE node merged is not the story having nothing
    left. Both callers go through the same predicate for this reason."""
    from app.services import story_landing
    from kernel.worker.landing import park_for_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, gid, eid, nid = await _coding_story(conn)
            rid2 = uuid.uuid4().hex
            await conn.execute(
                "INSERT INTO git_repos (id, owner, name) VALUES ($1,$2,$3)",
                rid2, "acme", f"repo-{rid2[:6]}")
            nid2 = uuid.uuid4().hex
            await conn.execute(
                "INSERT INTO git_nodes (id, git_repo_id, goal_id, "
                "branch_name, state) VALUES ($1,$2,$3,$4,'BRANCH_CREATED')",
                nid2, rid2, gid, f"feat/{sid}-two")
            await conn.execute(
                "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
                "VALUES ($1,$2,2)", nid2, sid)
            await park_for_landing(conn, eid)

            # First repo lands. The engine calls in naming this node.
            await conn.execute(
                "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)
            assert await story_landing.on_node_landed(
                _PoolOnConn(conn), nid) == 0, (
                "the run was released on its FIRST merge; the second repo's "
                "branch is still open and the work is not all on trunk")

            # Second lands. Now it is finished.
            await conn.execute(
                "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid2)
            assert await story_landing.on_node_landed(
                _PoolOnConn(conn), nid2) == 1


@pytest.mark.asyncio
async def test_a_story_that_lands_after_its_pod_was_reclaimed_still_finishes(
        monkeypatch):
    """THE COLD PATH. The deadline janitor stands the run down and the
    story keeps waiting, which is honest -- the PR really is open. If
    somebody merges it a week later there is no park and no run, so the
    hot path cannot see it, and without this it would sit at
    `awaiting_merge` for ever having actually landed."""
    from app.services import story_landing
    from kernel.worker.landing import park_for_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, _, eid, nid = await _coding_story(conn)
            await park_for_landing(conn, eid)
            await story_landing.sweep_entering(_PoolOnConn(conn))

            # The janitor stands the run down: park retired, run cancelled.
            await conn.execute(
                "UPDATE kernel.questions SET status = 'ANSWERED', "
                "answer_text = 'timed out' WHERE execution_id = $1", eid)
            await conn.execute(
                "UPDATE executions SET state = 'CANCELLED' WHERE id = $1", eid)

            # A week later, somebody merges it.
            await conn.execute(
                "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)
            assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 1
            assert await conn.fetchval(
                "SELECT state FROM stories WHERE id = $1", sid) == "done"


@pytest.mark.asyncio
async def test_a_waiting_story_lets_its_node_reach_ready(monkeypatch):
    """THE DEADLOCK, and it is the one that actually bit on dev.

    `advance_node_lifecycle` promotes a node to READY only when every
    member story is SETTLED, and READY is what gets the node queued and
    its PR opened. `awaiting_merge` was not in that set, so:

        the STORY waited at `awaiting_merge` for its work to land,
        the NODE waited at IN_PROGRESS for the story to be `done`,

    neither moved, and no PR was ever opened. Seen on caf071a4: branch
    pushed and 1 commit ahead of trunk, node IN_PROGRESS, merge queue
    empty, no PR.

    IT IS A `NOT IN` INSIDE A `NOT EXISTS`, which is why the sweep for
    inclusion lists missed it — the double negative makes an exclusion
    list behave as an inclusion list.
    """
    from app.services.git_graph.branches import advance_node_lifecycle
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, _, _, nid = await _coding_story(conn)
            await conn.execute(
                "UPDATE git_nodes SET state = 'IN_PROGRESS' WHERE id = $1", nid)
            await conn.execute(
                "UPDATE stories SET state = 'awaiting_merge' WHERE id = $1", sid)

            await advance_node_lifecycle(conn)
            assert await conn.fetchval(
                "SELECT state FROM git_nodes WHERE id = $1", nid) == "READY", (
                "the node is stuck at IN_PROGRESS while its story waits for a "
                "merge — no READY means no queue, no PR, and the story waits "
                "for a landing that cannot be arranged")


@pytest.mark.asyncio
async def test_a_story_still_being_worked_does_not_reach_ready(monkeypatch):
    """The carve-out is for work that is FINISHED and waiting to land,
    not for work in progress."""
    from app.services.git_graph.branches import advance_node_lifecycle
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, _, _, nid = await _coding_story(conn)
            await conn.execute(
                "UPDATE git_nodes SET state = 'IN_PROGRESS' WHERE id = $1", nid)
            await conn.execute(
                "UPDATE stories SET state = 'in_progress' WHERE id = $1", sid)
            await advance_node_lifecycle(conn)
            assert await conn.fetchval(
                "SELECT state FROM git_nodes WHERE id = $1", nid) == "IN_PROGRESS"


def test_deferring_the_merge_card_puts_the_claim_back():
    """A DEFERRAL IS NOT A FAILURE, and the poller cannot tell them apart.

    `_pending`'s loop stamps `card_posted_at` BEFORE calling `_post` --
    deliberately, so a post that fails leaves a card nobody sees rather
    than a question announced twice. The merge card defers on purpose
    (no PR yet), and without putting the claim back the row is never
    offered again: the card is lost for good.

    BMIA1 run 97: landing question 230 stamped at 12:57:37, PR #21
    opened seconds later, no card ever posted.
    """
    import inspect

    from app.services import kernel_park_cards
    src = inspect.getsource(kernel_park_cards._post)
    i = src.index('row["kind"] == "landing"')
    arm = src[i:src.index('elif row["kind"] == "closeout"', i)]
    assert "card_posted_at = NULL" in arm, (
        "the merge card defers without releasing its claim, so the "
        "poller never offers it again and no card is ever posted")
    assert 'row["q_status"] != "OPEN"' in arm, (
        "a landing park that is already answered -- by the merge, or by "
        "the platform retiring it to integrate -- still gets a card "
        "telling the person to merge a PR nobody is waiting on")


def test_a_story_can_reach_done_from_awaiting_merge():
    """THE INCLUSION-LIST TRAP, IN ITS THIRD SHAPE.

    `on_story_done`'s CAS guard is a PYTHON TUPLE, not SQL -- so the
    sweep that found the `state IN (...)` sites never saw it, and
    neither did the one that found `NOT EXISTS (... NOT IN ...)`.
    Without `awaiting_merge` in it the move to `done` is refused in
    silence at the very last step: node MERGED, PR merged, closeout
    answered, review running, story still `awaiting_merge`, nothing in
    error anywhere.

    BMIA1 caf071a4 survived it only because `_sweep_cold` -- written for
    a story that lands after its pod was released -- happens to match
    the same rows. A backstop covering for the primary path is not the
    arrangement anybody chose, and the two record different reasons for
    the move.
    """
    import inspect

    from app.services import go_wake_events
    src = inspect.getsource(go_wake_events.on_story_done)
    i = src.index("only_from=(")
    guard = src[i:src.index(")", i) + 1]
    assert "awaiting_merge" in guard, (
        "a coding story cannot reach `done` from `awaiting_merge`, which "
        "is the only state it can be in by the time this fires")


@pytest.mark.asyncio
async def test_the_cold_sweep_will_not_finish_a_story_with_a_live_run(
        monkeypatch):
    """IT SAID "no live run" ALL ALONG AND NEVER CHECKED.

    "No open park" is true for an instant whenever one park is being
    swapped for another, and in that instant this would finish a story
    whose run was about to ask the person whether it was finished.

    a4098b67: the trunk sweep retired the landing park, the merge
    landed in the same breath, `release` found no park to answer, and
    the cold sweep moved the story to `done` before the closeout card
    could be opened. The person was never asked.
    """
    from app.services import story_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, _, eid, nid = await _coding_story(conn)
            await conn.execute(
                "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)
            await conn.execute(
                "UPDATE stories SET state = 'awaiting_merge' WHERE id = $1",
                sid)
            # Parked, mid-swap: no open park for an instant.
            await conn.execute(
                "UPDATE kernel.executions SET state = 'PARKED_HOT' "
                " WHERE id = $1", eid)

            assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 0, (
                "a story with a live run was finished by the cold sweep — "
                "its closeout card would never be asked")
            assert await conn.fetchval(
                "SELECT state FROM stories WHERE id = $1", sid
            ) == "awaiting_merge"


@pytest.mark.asyncio
async def test_the_cold_sweep_still_finishes_a_story_with_no_run(monkeypatch):
    """The case it exists for must keep working."""
    from app.services import story_landing
    with _flag_on(monkeypatch):
        async with _conn() as conn:
            sid, _, eid, nid = await _coding_story(conn)
            await conn.execute(
                "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)
            await conn.execute(
                "UPDATE stories SET state = 'awaiting_merge' WHERE id = $1",
                sid)
            await conn.execute(
                "UPDATE kernel.executions SET state = 'CANCELLED' "
                " WHERE id = $1", eid)
            assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 1
            assert await conn.fetchval(
                "SELECT state FROM stories WHERE id = $1", sid) == "done"
