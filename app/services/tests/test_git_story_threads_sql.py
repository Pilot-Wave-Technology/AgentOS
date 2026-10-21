"""Engine notices go to the merged story's thread, and close when it lands.

WHAT WAS WRONG. `_notify_pr_opened` resolved the assignee's SHCA thread with
`ORDER BY t.created_at LIMIT 1`. That was correct while a person had exactly one
assistant thread per goal — "oldest" was "only". Stage K gave every story its own
thread, so the query silently became "the first story they ever opened in this
goal", and every merge notice for every branch landed in that one conversation.
Observed on KER15: seven merge stories, seven engine-made threads, and notices
arriving nowhere near the story being merged.

The join that fixes it already existed in the schema: `threads.story_id` says
what a conversation is about, `git_node_stories` says what a branch carries.

THE TWO SHAPES THAT MAKE THIS HARD, both pinned below:

  * ONE NODE, SEVERAL STORIES — a branch named after a dependency is a shared
    workstream and its merge lands everyone's work at once. Every member is
    told; picking one would silently drop the others.
  * ONE STORY, SEVERAL NODES — a multi-repo change. Each node's notice lands on
    that story's single thread, and the thread must stay OPEN until the last
    repo merges. Closing on the first would take the conversation away in the
    middle of the change.

**How to run.** Needs the local stack's PostgreSQL. Everything happens in one
transaction that is always rolled back.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.services.git_graph.story_threads import (
    close_threads_for_landed,
    landed_stories_for_node,
    threads_for_node,
)
from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

asyncpg = pytest.importorskip("asyncpg")

P = "zz-gitthread"


@contextlib.asynccontextmanager
async def _fixture():
    """A goal, a repo, and whatever the test adds. Always rolled back."""
    conn = await _connect_or_skip()
    for t in ("git_nodes", "git_node_stories", "git_repos"):
        if not await conn.fetchval(
                "SELECT 1 FROM information_schema.tables WHERE table_name=$1", t):
            await conn.close()
            pytest.skip(f"no {t} table — the git engine is not migrated here")
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
            f"{P}-goal", "git thread fixture", f"{P}-user")
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name, clone_url, trunk_branch) "
            "VALUES ($1,'acme','svc','https://x/y','main')", f"{P}-repo")
        yield conn
    finally:
        await tx.rollback()
        left = await conn.fetchval(
            "SELECT count(*) FROM goals WHERE id LIKE $1", f"{P}%")
        await conn.close()
        assert left == 0, "fixture survived the rollback — dev DB dirtied"


async def _story(conn, sid, *, by=f"{P}-user", kind="code"):
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, created_by, assigned_to, "
        " story_kind) VALUES ($1,$2,$3,$4,$5,$6)",
        sid, f"{P}-goal", f"title {sid}", by, f"{P}-user", kind)


async def _thread(conn, tid, sid):
    await conn.execute(
        "INSERT INTO threads (id, goal_id, agent_id, story_id, created_by, "
        " title) VALUES ($1,$2,$3,$4,$5,$6)",
        tid, f"{P}-goal", f"{P}-assistant", sid, f"{P}-user", f"t {sid}")


async def _node(conn, nid, state="READY"):
    await conn.execute(
        "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state, "
        " trunk_branch) VALUES ($1,$2,$3,$4,$5,'main')",
        nid, f"{P}-repo", f"{P}-goal", f"feat/{nid}", state)


async def _member(conn, nid, sid, seq=0):
    await conn.execute(
        "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
        "VALUES ($1,$2,$3)", nid, sid, seq)


def test_the_notice_goes_to_the_merged_storys_thread():
    """The bug, directly: an OLDER unrelated thread must not win."""
    async def run():
        async with _fixture() as conn:
            # An older thread for a different story — what the old
            # `ORDER BY created_at LIMIT 1` would have picked.
            await _story(conn, f"{P}-other")
            await _thread(conn, f"{P}-t-other", f"{P}-other")

            await _story(conn, f"{P}-s1")
            await _thread(conn, f"{P}-t1", f"{P}-s1")
            await _node(conn, f"{P}-n1")
            await _member(conn, f"{P}-n1", f"{P}-s1")

            got = [t["thread_id"] for t in await threads_for_node(conn, f"{P}-n1")]
            assert got == [f"{P}-t1"], got
    asyncio.run(run())


def test_a_shared_workstream_tells_every_member():
    """One branch, two stories. Both land; both are told."""
    async def run():
        async with _fixture() as conn:
            await _node(conn, f"{P}-n1")
            for s in ("a", "b"):
                await _story(conn, f"{P}-{s}")
                await _thread(conn, f"{P}-t-{s}", f"{P}-{s}")
                await _member(conn, f"{P}-n1", f"{P}-{s}")

            got = sorted(t["thread_id"] for t in
                         await threads_for_node(conn, f"{P}-n1"))
            assert got == [f"{P}-t-a", f"{P}-t-b"], got
    asyncio.run(run())


def test_engine_stories_are_not_conversations():
    """A diff/fix story the engine spawned is machinery, not somebody's chat —
    its notices belong on the thread of the work it is landing."""
    async def run():
        async with _fixture() as conn:
            await _node(conn, f"{P}-n1")
            await _story(conn, f"{P}-eng", by="engine")
            await _thread(conn, f"{P}-t-eng", f"{P}-eng")
            await _member(conn, f"{P}-n1", f"{P}-eng")
            assert await threads_for_node(conn, f"{P}-n1") == []
    asyncio.run(run())


def test_a_multi_repo_story_keeps_its_thread_until_the_last_repo_lands():
    """The case the user asked for by name. Closing on the first merge would
    take the conversation away in the middle of the change."""
    async def run():
        async with _fixture() as conn:
            await _story(conn, f"{P}-s1")
            await _thread(conn, f"{P}-t1", f"{P}-s1")
            await _node(conn, f"{P}-n1", state="MERGED")
            await _node(conn, f"{P}-n2", state="READY")      # second repo, live
            await _member(conn, f"{P}-n1", f"{P}-s1")
            await _member(conn, f"{P}-n2", f"{P}-s1")

            assert await landed_stories_for_node(conn, f"{P}-n1") == []
            assert await close_threads_for_landed(conn, f"{P}-n1") == []
            still = await conn.fetchval(
                "SELECT left_goal_at FROM threads WHERE id=$1", f"{P}-t1")
            assert still is None, "thread closed while a repo was still open"

            # The last repo lands.
            await conn.execute(
                "UPDATE git_nodes SET state='MERGED' WHERE id=$1", f"{P}-n2")
            closed = await close_threads_for_landed(conn, f"{P}-n2")
            assert closed == [f"{P}-t1"], closed
            gone = await conn.fetchval(
                "SELECT left_goal_at FROM threads WHERE id=$1", f"{P}-t1")
            assert gone is not None
    asyncio.run(run())


def test_a_story_whose_branches_were_all_abandoned_is_not_landed():
    """ABANDONED settles a node so an orphan cannot hold a thread open for
    ever — but nothing reached trunk, so this is not a story that landed."""
    async def run():
        async with _fixture() as conn:
            await _story(conn, f"{P}-s1")
            await _thread(conn, f"{P}-t1", f"{P}-s1")
            await _node(conn, f"{P}-n1", state="ABANDONED")
            await _member(conn, f"{P}-n1", f"{P}-s1")
            assert await landed_stories_for_node(conn, f"{P}-n1") == []
            assert await close_threads_for_landed(conn, f"{P}-n1") == []
    asyncio.run(run())


def test_an_abandoned_sibling_does_not_hold_the_thread_open():
    """One repo merged, one torn down. The story is finished with the engine."""
    async def run():
        async with _fixture() as conn:
            await _story(conn, f"{P}-s1")
            await _thread(conn, f"{P}-t1", f"{P}-s1")
            await _node(conn, f"{P}-n1", state="MERGED")
            await _node(conn, f"{P}-n2", state="ABANDONED")
            await _member(conn, f"{P}-n1", f"{P}-s1")
            await _member(conn, f"{P}-n2", f"{P}-s1")
            assert await close_threads_for_landed(conn, f"{P}-n1") == [f"{P}-t1"]
    asyncio.run(run())


def test_a_closed_thread_is_not_a_notice_target_and_closes_once():
    """After closing, the thread is invisible to the resolver — so a later
    notice falls through to the bell instead of posting into a dead room. And
    re-running the close is a no-op, not a second archive."""
    async def run():
        async with _fixture() as conn:
            await _story(conn, f"{P}-s1")
            await _thread(conn, f"{P}-t1", f"{P}-s1")
            await _node(conn, f"{P}-n1", state="MERGED")
            await _member(conn, f"{P}-n1", f"{P}-s1")

            assert await close_threads_for_landed(conn, f"{P}-n1") == [f"{P}-t1"]
            assert await threads_for_node(conn, f"{P}-n1") == []
            assert await close_threads_for_landed(conn, f"{P}-n1") == []
    asyncio.run(run())
