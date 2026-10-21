"""Merged is not finished. A person gets one last say, and it costs them.

CARD [B]. The work is on trunk and the workspace is STILL UP — which is
the only moment at which "actually, one more thing" can continue THIS
story instead of opening another against a cold checkout. Closing the
story is what releases the tree, so the card is a decision with a price
rather than a formality.

THE TRAP THIS PHASE WALKED INTO, pinned below: `_finalize_merged` closed
the story's thread the moment its last node merged. That is exactly when
this card is posted, and it is posted INTO that thread — so the
conversation was being retired seconds before the question was asked in
it, and "more work" would have had nowhere to be said.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

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


async def _landed_story(conn, *, state: str = "awaiting_merge"):
    """A story whose only node has merged, with a thread and a parked run."""
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid, tid, rid = f"{sid}-goal", f"{sid}-thread", uuid.uuid4().hex
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO threads (id, goal_id, title, created_by, story_id) "
        "VALUES ($1,$2,$3,$4,$5)", tid, gid, "story thread",
        f"{PREFIX}-user", sid if False else None)
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state) "
        "VALUES ($1,$2,$3,'person',$4)", sid, gid, "landed story", state)
    await conn.execute("UPDATE threads SET story_id = $2 WHERE id = $1",
                       tid, sid)
    await conn.execute(
        "INSERT INTO git_repos (id, owner, name) VALUES ($1,$2,$3)",
        rid, "acme", f"repo-{rid[:6]}")
    nid = uuid.uuid4().hex
    await conn.execute(
        "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state, "
        "cycle) VALUES ($1,$2,$3,$4,'MERGED',1)",
        nid, rid, gid, f"feat/{sid}")
    await conn.execute(
        "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
        "VALUES ($1,$2,1)", nid, sid)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, kind, domain, "
        "state, generation) VALUES ($1,$2,'story','code','PARKED_HOT',4) "
        "RETURNING id", gid, sid)
    return sid, gid, tid, eid, nid


# ── the card ─────────────────────────────────────────────────────────

def test_only_a_person_may_close_a_story():
    """Done-or-more-work is the decision that was deliberately kept."""
    from kernel.mailbox.schema import needs_a_person
    assert needs_a_person("closeout") is True
    assert needs_a_person("closeout", go_driven=True) is True, (
        "an autonomous errand's GO may settle a PLAN alone; it must never "
        "be able to close a story")


def test_a_closeout_must_say_what_landed():
    """A card asking somebody to call something finished has to show them
    what it is."""
    from kernel.mailbox.schema import validate_question
    bad = validate_question("closeout", "done?", {})
    assert bad is not None and bad.code == "E_NO_SUMMARY"
    assert validate_question("closeout", "done?", {"summary": "it landed"}) is None


def test_the_card_offers_only_closing():
    """CHANGED 2026-10-14. It used to offer "Ask for more work" beside
    "Mark story done", on the reasoning that the workspace was still up
    so anything missed could be added to THIS story. Abhishek ruled the
    other way and it is the better rule: "more work should always be
    done before merging — that is how work happens everywhere." The
    merge is the commitment; asking after it reopens work somebody has
    already accepted onto trunk.

    It was also the only thing that needed a merged branch brought back
    to life, and that is where it broke — the node is reset, the create
    pass re-stamps `base_sha` with the live trunk tip, and the branch,
    never actually re-cut, sits behind by its own merge commit while
    the bookkeeping calls it level. No pull request, no catch-up."""
    from app.services.kernel_park_cards import build_card
    card = build_card({"kind": "closeout", "id": 1, "execution_id": 1,
                       "goal_id": "g", "story_id": "s",
                       "question_text": "done?"}, None, "")
    assert card["needs_human"] is True
    assert [o["value"] for o in card["options"]] == ["approve"]
    assert "Mark story done" in card["options"][0]["label"], (
        "the post-merge card must say exactly what an errand's card says "
        "— finishing means one thing on this platform, and a coding "
        "story simply reaches this card later, once its work is on trunk")


def test_the_LANDING_card_still_offers_more_work():
    """Before the merge is where it belongs, and removing it there
    would leave no way to add to a story at all."""
    from app.services.kernel_park_cards import build_card
    card = build_card({"kind": "landing", "id": 1, "execution_id": 1,
                       "goal_id": "g", "story_id": "s",
                       "question_text": "merge?"}, None, "")
    assert "revise" in [o["value"] for o in card["options"]]


def test_the_card_says_what_closing_costs():
    """A person who closes the story without being told they are
    releasing the workspace has not been told what they are spending."""
    from app.services.kernel_park_cards import _closeout_body
    body = _closeout_body({"summary": "the thing", "branches": ["feat/x"]})
    assert "releases the workspace" in body
    assert "feat/x" in body
    assert "THIS story" in body or "this story" in body.lower()


# ── the thread that must stay open ───────────────────────────────────

@pytest.mark.asyncio
async def test_the_merge_does_not_close_the_thread_the_card_needs():
    """THE CONFLICT THIS PHASE FOUND. The card is posted into this
    thread; closing it at the merge retires the conversation the
    question is about to be asked in."""
    from app.services.git_graph.story_threads import close_threads_for_landed
    async with _conn() as conn:
        sid, _, tid, _, nid = await _landed_story(conn)
        closed = await close_threads_for_landed(_PoolOnConn(conn), nid)
        assert closed == [], (
            "the thread was closed while the story is still awaiting_merge "
            "— the closeout card would be posted into an archived thread")
        assert await conn.fetchval(
            "SELECT left_goal_at FROM threads WHERE id = $1", tid) is None


@pytest.mark.asyncio
async def test_the_thread_closes_when_the_story_actually_finishes():
    from app.services.git_graph.story_threads import (
        close_thread_for_finished_story)
    async with _conn() as conn:
        sid, _, tid, _, _ = await _landed_story(conn, state="done")
        closed = await close_thread_for_finished_story(_PoolOnConn(conn), sid)
        assert closed == [tid]


@pytest.mark.asyncio
async def test_a_story_that_never_landed_keeps_its_thread():
    """The errand and cancel paths own their own closures; widening this
    to "any finished story" would quietly take both over."""
    from app.services.git_graph.story_threads import (
        close_thread_for_finished_story)
    async with _conn() as conn:
        sid, _, _, _, nid = await _landed_story(conn, state="done")
        await conn.execute(
            "UPDATE git_nodes SET state = 'ABANDONED' WHERE id = $1", nid)
        assert await close_thread_for_finished_story(
            _PoolOnConn(conn), sid) == []


# ── more work, after the merge, needs somewhere to go ────────────────

@pytest.mark.asyncio
async def test_more_work_after_a_merge_reopens_the_same_branch():
    """ONE FEATURE BRANCH PER STORY. A new branch would mean a new story,
    and the schema agrees: git_nodes carries UNIQUE (git_repo_id,
    branch_name), so a second node naming this branch cannot be inserted
    at all. The node is reopened instead."""
    from app.services.git_graph.node_cycles import reopen_for_next_cycle
    async with _conn() as conn:
        sid, gid, _, _, nid = await _landed_story(conn)
        await conn.execute(
            "UPDATE git_nodes SET pr_number = 7, merge_commit_sha = 'abc', "
            "base_sha = 'old' WHERE id = $1", nid)

        assert await reopen_for_next_cycle(conn, sid) == [nid]
        node = await conn.fetchrow(
            "SELECT branch_name, state, cycle, pr_number, base_sha, "
            "       merge_commit_sha, mergeable FROM git_nodes WHERE id = $1",
            nid)
        assert node["branch_name"] == f"feat/{sid}", "the branch was renamed"
        assert node["state"] == "PLANNED"
        assert node["cycle"] == 2
        assert node["pr_number"] is None
        assert node["merge_commit_sha"] is None
        assert node["mergeable"] == "unknown"
        assert node["base_sha"] is None, (
            "the old base_sha survived — the engine would compare this "
            "round against the trunk of the PREVIOUS cut and report work "
            "that has not landed as already landed")


@pytest.mark.asyncio
async def test_reopening_keeps_the_record_of_what_already_landed():
    """Resetting the node clears the columns that say what round one
    was. Without somewhere to put them, a story that went round twice is
    indistinguishable from one that went round once."""
    from app.services.git_graph.node_cycles import reopen_for_next_cycle
    async with _conn() as conn:
        sid, _, _, _, nid = await _landed_story(conn)
        await conn.execute(
            "UPDATE git_nodes SET pr_number = 7, merge_commit_sha = 'abc', "
            "base_sha = 'old' WHERE id = $1", nid)
        await reopen_for_next_cycle(conn, sid)

        landing = await conn.fetchrow(
            "SELECT cycle, branch_name, pr_number, merge_commit_sha, base_sha "
            "FROM git_node_landings WHERE git_node_id = $1", nid)
        assert landing["cycle"] == 1
        assert landing["pr_number"] == 7
        assert landing["merge_commit_sha"] == "abc"
        assert landing["base_sha"] == "old"
        assert landing["branch_name"] == f"feat/{sid}"


@pytest.mark.asyncio
async def test_reopening_twice_changes_nothing_the_second_time():
    """The landings PK conflicts and the reset is guarded on MERGED.
    Neither needs a flag somebody has to clear."""
    from app.services.git_graph.node_cycles import reopen_for_next_cycle
    async with _conn() as conn:
        sid, _, _, _, nid = await _landed_story(conn)
        assert await reopen_for_next_cycle(conn, sid) == [nid]
        assert await reopen_for_next_cycle(conn, sid) == []
        assert await conn.fetchval(
            "SELECT cycle FROM git_nodes WHERE id = $1", nid) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM git_node_landings WHERE git_node_id = $1",
            nid) == 1


@pytest.mark.asyncio
async def test_the_merge_story_comes_back_for_approval():
    """A merge story is the human approval for a landing, and it is keyed
    on the merge GROUP rather than the cycle. Round one's is `done` and
    reconcile only revives a `proposed` one — so without this, round two
    reaches READY with nothing to approve and waits for ever."""
    from app.services.git_graph.node_cycles import reopen_for_next_cycle
    async with _conn() as conn:
        sid, gid, _, _, nid = await _landed_story(conn)
        grp = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_merge_groups (id, goal_id) VALUES ($1,$2)",
            grp, gid)
        await conn.execute(
            "INSERT INTO git_merge_group_nodes (merge_group_id, git_node_id) "
            "VALUES ($1,$2)", grp, nid)
        msid = f"{PREFIX}-merge-{uuid.uuid4().hex[:6]}"
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, created_by, state, "
            "story_kind, merge_group_id, merge_approved_at) "
            "VALUES ($1,$2,'merge','engine','done','merge',$3, now())",
            msid, gid, grp)

        await reopen_for_next_cycle(conn, sid)
        merge = await conn.fetchrow(
            "SELECT state, merge_approved_at FROM stories WHERE id = $1", msid)
        assert merge["state"] == "proposed"
        assert merge["merge_approved_at"] is None, (
            "round two would land on an approval nobody gave it")


@pytest.mark.asyncio
async def test_no_reopen_while_a_sibling_repo_is_still_open():
    """A multi-repo story merges one repo at a time. Reopening the first
    while the second is in flight puts the group back in the queue
    halfway through the round it is still finishing."""
    from app.services.git_graph.node_cycles import reopen_for_next_cycle
    async with _conn() as conn:
        sid, gid, _, _, _ = await _landed_story(conn)
        rid2 = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name) VALUES ($1,$2,$3)",
            rid2, "acme", f"repo-{rid2[:6]}")
        nid2 = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, "
            "state, cycle) VALUES ($1,$2,$3,$4,'BRANCH_CREATED',1)",
            nid2, rid2, gid, f"feat/{sid}")
        await conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
            "VALUES ($1,$2,2)", nid2, sid)
        assert await reopen_for_next_cycle(conn, sid) == []


def test_the_merge_card_lists_every_pr_a_multi_repo_story_waits_on():
    """ONE OF TWO LOOKS COMPLETE AND IS NOT.

    A story may write several repos and they land as one group: the
    person has to merge each PR, and the story keeps waiting until the
    last is in. A card showing only the newest would be merged, nothing
    would happen, and nothing would say why.
    """
    from app.services.kernel_park_cards import _landing_body
    body = _landing_body(
        {"summary": "two repos"},
        [("https://github.com/acme/api/pull/7", "feat/x", "api"),
         ("https://github.com/acme/web/pull/9", "feat/x", "web")])
    assert "2 repositories" in body
    assert "pull/7" in body and "pull/9" in body
    assert "LAST of them" in body, (
        "nothing tells the person the story waits for all of them")


def test_a_single_repo_card_is_not_dressed_up_as_a_list():
    from app.services.kernel_park_cards import _landing_body
    body = _landing_body(
        {"summary": "one repo"},
        [("https://github.com/acme/api/pull/7", "feat/x", "api")])
    assert "repositories" not in body
    assert "Review and merge the pull request" in body
    assert "pull/7" in body
