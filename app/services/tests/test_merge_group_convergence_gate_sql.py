"""Multi-repo merge-group convergence gate (research/MULTI_REPO_STORIES_PLAN.md §15.4),
against a real database.

`enqueue_ready_merges` is the sole entry point into the live merge pipeline
(enqueue -> lease -> grind -> human merge -> detect; see engine.py:200-210). Before
this change it promoted any READY node straight to QUEUED with zero awareness of
sibling nodes in the same `git_merge_group_nodes` group -- a repo with no textual
conflict of its own could get its PR opened (and merged by a human) while a sibling
repo in the SAME multi-write story was still mid-work, with nobody having checked
whether the two changes are actually compatible. These tests build the exact
git_nodes / git_merge_group_nodes state by hand and assert the gate holds or
releases correctly, without relying on a live git engine tick.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.git_graph.merge_lane import enqueue_ready_merges
from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        leaked = await conn.fetchval("SELECT count(*) FROM goals WHERE id LIKE $1", f"{PREFIX}%")
        await conn.close()
        assert leaked == 0, f"{leaked} fixture rows survived rollback — dev DB dirtied"


async def _setup_goal(conn, goal_id: str) -> None:
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
        goal_id, f"fixture {goal_id}", f"{PREFIX}-user",
    )


async def _repo(conn, name: str) -> str:
    repo_id = f"{PREFIX}-repo-{name}"
    await conn.execute(
        "INSERT INTO git_repos (id, owner, name) VALUES ($1,'acme',$2) "
        "ON CONFLICT (id) DO NOTHING",
        repo_id, name,
    )
    return repo_id


async def _node(conn, node_id: str, repo_id: str, goal_id: str, branch: str, state: str) -> None:
    await conn.execute(
        "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state) "
        "VALUES ($1,$2,$3,$4,$5)",
        node_id, repo_id, goal_id, branch, state,
    )


async def _group(conn, group_id: str, goal_id: str, node_ids: list[str]) -> None:
    await conn.execute(
        "INSERT INTO git_merge_groups (id, goal_id) VALUES ($1,$2)", group_id, goal_id,
    )
    for seq, nid in enumerate(node_ids):
        await conn.execute(
            "INSERT INTO git_merge_group_nodes (merge_group_id, git_node_id, seq) VALUES ($1,$2,$3)",
            group_id, nid, seq,
        )


async def _merge_story(conn, story_id: str, goal_id: str, group_id: str, thread_id: str) -> None:
    await conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title) VALUES ($1,$2,$3,$4)",
        thread_id, goal_id, f"{PREFIX}-user", "merge thread",
    )
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, thread_id, created_by, "
        "  story_kind, merge_group_id) "
        "VALUES ($1,$2,'Merge fixture','proposed',$3,'engine','merge',$4)",
        story_id, goal_id, thread_id, group_id,
    )


async def _node_state(conn, node_id: str) -> str:
    return await conn.fetchval("SELECT state FROM git_nodes WHERE id = $1", node_id)


@pytest.mark.asyncio
async def test_single_node_group_unaffected():
    """The common single-repo case: no sibling row exists at all, so the new
    NOT EXISTS clause is trivially true — byte-identical to before the gate existed."""
    async with _conn() as conn:
        gid = f"{PREFIX}-single-goal"
        await _setup_goal(conn, gid)
        repo = await _repo(conn, "svc-a")
        node = f"{PREFIX}-single-node"
        await _node(conn, node, repo, gid, "feat/x", "READY")
        group = f"{PREFIX}-single-group"
        await _group(conn, group, gid, [node])
        await _merge_story(conn, f"{PREFIX}-single-merge", gid, group, f"{PREFIX}-single-thread")

        enq = await enqueue_ready_merges(conn)

        assert enq == 1
        assert await _node_state(conn, node) == "QUEUED"


@pytest.mark.asyncio
async def test_two_node_group_both_ready_both_enqueue():
    """Multi-write story, both repos already caught up with their own work — the
    group has converged, so both nodes enqueue together in the same pass."""
    async with _conn() as conn:
        gid = f"{PREFIX}-both-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b = await _repo(conn, "svc-a"), await _repo(conn, "svc-b")
        node_a, node_b = f"{PREFIX}-both-a", f"{PREFIX}-both-b"
        await _node(conn, node_a, repo_a, gid, "feat/x", "READY")
        await _node(conn, node_b, repo_b, gid, "feat/x", "READY")
        group = f"{PREFIX}-both-group"
        await _group(conn, group, gid, [node_a, node_b])
        await _merge_story(conn, f"{PREFIX}-both-merge", gid, group, f"{PREFIX}-both-thread")

        enq = await enqueue_ready_merges(conn)

        assert enq == 2
        assert await _node_state(conn, node_a) == "QUEUED"
        assert await _node_state(conn, node_b) == "QUEUED"


@pytest.mark.asyncio
async def test_ready_node_held_while_sibling_still_in_progress():
    """The exact defect this gate closes: repo A has nothing left to do (READY) but
    repo B in the same multi-write story is still IN_PROGRESS — A must NOT enqueue
    (and therefore cannot open/merge its PR) until B catches up."""
    async with _conn() as conn:
        gid = f"{PREFIX}-hold-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b = await _repo(conn, "svc-a"), await _repo(conn, "svc-b")
        node_a, node_b = f"{PREFIX}-hold-a", f"{PREFIX}-hold-b"
        await _node(conn, node_a, repo_a, gid, "feat/x", "READY")
        await _node(conn, node_b, repo_b, gid, "feat/x", "IN_PROGRESS")
        group = f"{PREFIX}-hold-group"
        await _group(conn, group, gid, [node_a, node_b])
        await _merge_story(conn, f"{PREFIX}-hold-merge", gid, group, f"{PREFIX}-hold-thread")

        enq = await enqueue_ready_merges(conn)

        assert enq == 0
        assert await _node_state(conn, node_a) == "READY"
        assert await _node_state(conn, node_b) == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_ready_node_held_while_sibling_escalated():
    """ESCALATED counts as "not converged", same treatment merge_lane.py:153 /
    lease.py:76 already give it elsewhere (it's recoverable via Retry, not terminal) —
    a sibling stuck ESCALATED must not silently wave the rest of the group through."""
    async with _conn() as conn:
        gid = f"{PREFIX}-esc-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b = await _repo(conn, "svc-a"), await _repo(conn, "svc-b")
        node_a, node_b = f"{PREFIX}-esc-a", f"{PREFIX}-esc-b"
        await _node(conn, node_a, repo_a, gid, "feat/x", "READY")
        await _node(conn, node_b, repo_b, gid, "feat/x", "ESCALATED")
        group = f"{PREFIX}-esc-group"
        await _group(conn, group, gid, [node_a, node_b])
        await _merge_story(conn, f"{PREFIX}-esc-merge", gid, group, f"{PREFIX}-esc-thread")

        enq = await enqueue_ready_merges(conn)

        assert enq == 0
        assert await _node_state(conn, node_a) == "READY"


@pytest.mark.asyncio
async def test_ready_node_not_held_by_already_merged_sibling():
    """A sibling that already landed (MERGED) is settled, not "still converging" —
    it must not block the rest of the group from enqueueing (this is the ordinary,
    expected case: one repo lands before another within the same multi-write story)."""
    async with _conn() as conn:
        gid = f"{PREFIX}-merged-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b = await _repo(conn, "svc-a"), await _repo(conn, "svc-b")
        node_a, node_b = f"{PREFIX}-merged-a", f"{PREFIX}-merged-b"
        await _node(conn, node_a, repo_a, gid, "feat/x", "READY")
        await _node(conn, node_b, repo_b, gid, "feat/x", "MERGED")
        group = f"{PREFIX}-merged-group"
        await _group(conn, group, gid, [node_a, node_b])
        await _merge_story(conn, f"{PREFIX}-merged-merge", gid, group, f"{PREFIX}-merged-thread")

        enq = await enqueue_ready_merges(conn)

        assert enq == 1
        assert await _node_state(conn, node_a) == "QUEUED"


@pytest.mark.asyncio
async def test_ready_node_not_held_by_abandoned_sibling():
    """An ABANDONED sibling (e.g. a deleted-goal reap) is terminal, not "still
    converging" — must not permanently strand a healthy sibling in the same group."""
    async with _conn() as conn:
        gid = f"{PREFIX}-aband-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b = await _repo(conn, "svc-a"), await _repo(conn, "svc-b")
        node_a, node_b = f"{PREFIX}-aband-a", f"{PREFIX}-aband-b"
        await _node(conn, node_a, repo_a, gid, "feat/x", "READY")
        await _node(conn, node_b, repo_b, gid, "feat/x", "ABANDONED")
        group = f"{PREFIX}-aband-group"
        await _group(conn, group, gid, [node_a, node_b])
        await _merge_story(conn, f"{PREFIX}-aband-merge", gid, group, f"{PREFIX}-aband-thread")

        enq = await enqueue_ready_merges(conn)

        assert enq == 1
        assert await _node_state(conn, node_a) == "QUEUED"
