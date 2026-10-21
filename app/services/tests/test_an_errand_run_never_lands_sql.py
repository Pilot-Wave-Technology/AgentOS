"""A story RUN as an errand does not land, though the story is unchanged.

The assistant may start a story as an errand. The story keeps its shape;
the run carries the errand. The wake at DONE reads the run and deletes
the errand's branch -- so the landing gate must read the run too, or it
queues a merge of a branch that no longer exists. Measured on GOAL2 /
e64f5e80: GitHub 404 on compare, node ESCALATED, and a card asking a
human to push a branch that had been deleted on purpose.

Real database, one transaction, always rolled back.
"""
from __future__ import annotations

import pytest

from app.services.git_graph.merge_lane import enqueue_ready_merges
from app.services.tests.test_merge_group_convergence_gate_sql import (
    _conn, _group, _merge_story, _node, _node_state, _repo, _setup_goal)
from app.services.tests.test_story_repo_scope_sql import PREFIX


async def _code_story_with_run(conn, gid: str, node: str, run_kind: str) -> str:
    sid = f"{PREFIX}-run-{run_kind}"
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'s','done',$3)", sid, gid, f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
        "VALUES ($1,$2,0)", node, sid)
    await conn.execute(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, "
        "state, task) VALUES ($1, $2, $3, $4, 'DONE', 't')",
        run_kind, "ops" if run_kind == "errand" else "coding", gid, sid)
    return sid


async def _ready_node(conn, tag: str) -> tuple[str, str]:
    gid = f"{PREFIX}-{tag}-goal"
    await _setup_goal(conn, gid)
    repo = await _repo(conn, f"svc-{tag}")
    node = f"{PREFIX}-{tag}-node"
    await _node(conn, node, repo, gid, f"feat/{tag}", "READY")
    group = f"{PREFIX}-{tag}-group"
    await _group(conn, group, gid, [node])
    await _merge_story(conn, f"{PREFIX}-{tag}-merge", gid, group,
                       f"{PREFIX}-{tag}-thread")
    return gid, node


@pytest.mark.asyncio
async def test_a_story_run_as_an_errand_is_held_at_ready():
    async with _conn() as conn:
        gid, node = await _ready_node(conn, "errandrun")
        sid = await _code_story_with_run(conn, gid, node, "errand")

        assert await enqueue_ready_merges(conn) == 0
        assert await _node_state(conn, node) == "READY"
        # The story's shape is the planner's, and it did not move.
        assert await conn.fetchval(
            "SELECT work_kind FROM stories WHERE id = $1", sid) is None


@pytest.mark.asyncio
async def test_a_story_run_as_a_story_still_lands():
    async with _conn() as conn:
        gid, node = await _ready_node(conn, "storyrun")
        await _code_story_with_run(conn, gid, node, "story")

        assert await enqueue_ready_merges(conn) == 1
        assert await _node_state(conn, node) == "QUEUED"
