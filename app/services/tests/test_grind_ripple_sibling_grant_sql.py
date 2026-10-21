"""Ripple-check sibling grant (research/MULTI_REPO_STORIES_PLAN.md §15.3), against a
real database.

When the grind loop spawns a "Sync" (diff) story for a repo that's behind trunk, that
story should be granted write access to every SIBLING repo in the same multi-write
story's merge group too — not just its own repo — so its one session can actually act
on a ripple effect it discovers, in the same story, without needing a second story or
a second assistant session. This reuses the existing multi-repo clone machinery
(`resolve_repos_for_story` -> `_clone_descriptors`) purely through extra
`story_resources` grants; no new provisioning code.

Safe from a two-writer race because every story spawned into one merge group shares
the SAME assignee (the merge story's `assigned_to`), so the existing
assistant-busy/single-active-thread rule already guarantees at most one of them is
ever actively dispatched at a time.
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from app.services.git_graph.grind import _ensure_diff_story
from app.services.tests.test_local_workspace_nodes_sql import _PoolShim
from app.services.tests.test_merge_group_convergence_gate_sql import (
    _group, _node, _repo, _setup_goal,
)
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


async def _branch_resource(conn, goal_id: str, owner: str, name: str, branch: str) -> str:
    rid = f"{PREFIX}-branch-{name}-{branch}"
    await conn.execute(
        "INSERT INTO goal_resources (id, goal_id, kind, role, name, created_by, status, "
        "  repo_owner, repo_name, branch_name) "
        "VALUES ($1,$2,'branch','feature',$3,$4,'active',$5,$6,$7)",
        rid, goal_id, f"{name}:{branch}", f"{PREFIX}-user", owner, name, branch,
    )
    return rid


async def _merge_story(conn, story_id: str, goal_id: str, group_id: str, thread_id: str,
                        assignee: str) -> None:
    await conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title) VALUES ($1,$2,$3,$4)",
        thread_id, goal_id, f"{PREFIX}-user", "merge thread",
    )
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, thread_id, created_by, "
        "  story_kind, merge_group_id, assigned_to) "
        "VALUES ($1,$2,'Merge fixture','proposed',$3,'engine','merge',$4,$5)",
        story_id, goal_id, thread_id, group_id, assignee,
    )


@pytest.mark.asyncio
async def test_spawned_diff_story_is_granted_write_access_to_sibling_repo():
    """The concrete ask: node A's Sync story, once spawned, is also granted a
    write ('produces') story_resources link to sibling node B's branch — enough
    for provision_story_workspace's existing multi-repo path to clone B too."""
    async with _conn() as conn:
        gid = f"{PREFIX}-sib-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b = await _repo(conn, "svc-a"), await _repo(conn, "svc-b")
        node_a, node_b = f"{PREFIX}-sib-a", f"{PREFIX}-sib-b"
        await _node(conn, node_a, repo_a, gid, "feat/x", "IN_PROGRESS")
        await _node(conn, node_b, repo_b, gid, "feat/x", "IN_PROGRESS")
        group = f"{PREFIX}-sib-group"
        await _group(conn, group, gid, [node_a, node_b])
        assignee = f"{PREFIX}-user"
        await _merge_story(conn, f"{PREFIX}-sib-merge", gid, group, f"{PREFIX}-sib-thread", assignee)
        await _branch_resource(conn, gid, "acme", "svc-a", "feat/x")
        br_b = await _branch_resource(conn, gid, "acme", "svc-b", "feat/x")

        h = {
            "node_id": node_a, "goal_id": gid, "owner": "acme", "name": "svc-a",
            "branch_name": "feat/x", "trunk_branch": "main", "git_repo_id": repo_a,
        }
        with patch("app.services.git_graph.grind.get_pool", lambda: _PoolShim(conn)):
            created = await _ensure_diff_story(h, "trunk-sha-1")

        assert created is True
        sid = await conn.fetchval(
            "SELECT id FROM stories WHERE git_node_id = $1 AND created_by = 'engine'",
            node_a,
        )
        assert sid is not None

        granted = await conn.fetch(
            "SELECT resource_id, relation FROM story_resources WHERE story_id = $1", sid,
        )
        granted_ids = {g["resource_id"] for g in granted}
        assert br_b in granted_ids, "sibling repo's branch resource was not granted"
        sib_relation = next(g["relation"] for g in granted if g["resource_id"] == br_b)
        # 'modifies', not 'produces' (2026-08-20 fix): both grants land in the same
        # DB transaction and tie exactly on created_at, so the sibling grant must be
        # a DIFFERENT relation value from the story's own repo, or the two are
        # indistinguishable to _resolve_repo_for_story / resolve_repos_for_story and
        # the sibling can resolve as primary/cwd instead of the story's own repo.
        # 'modifies' is still a full write relation (repo_identity.WRITE_RELATIONS).
        assert sib_relation == "modifies", "sibling grant must be write-capable, not read-only"

        brief = await conn.fetchval("SELECT description FROM stories WHERE id = $1", sid)
        assert "svc-b" in brief, "brief should tell the assistant about the sibling repo"
        assert "SAME session" in brief


@pytest.mark.asyncio
async def test_ripple_sibling_grant_excludes_an_already_terminal_sibling():
    """2026-08-20 fix: a sibling whose OWN node already reached a terminal state
    (MERGED/ABANDONED) — or ESCALATED (a human closed its PR unmerged, a
    persistent GitHub 401/403/404, or another grind-loop condition needing a
    human Retry) — must NOT be granted or mentioned in the brief. MERGED/
    ABANDONED confirmed live (goal VKSDN, story b20bb3f9): a sync story was
    granted write access to a sibling that had already merged — a branch the
    engine's own detection (detect.py's _POLL_SKIP_STATES) has stopped
    watching entirely, so any new commit pushed there is invisible to it (no
    PR-open detection, no finalize, and the branch may already be deleted by
    GitHub's auto-delete-on-merge). ESCALATED is the same story: its queue
    slot is already released and it needs a human decision, not a new commit
    from this session."""
    async with _conn() as conn:
        gid = f"{PREFIX}-sib-terminal-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b, repo_c, repo_d = (
            await _repo(conn, "svc-a"), await _repo(conn, "svc-b"),
            await _repo(conn, "svc-c"), await _repo(conn, "svc-d"),
        )
        node_a, node_b, node_c, node_d = (
            f"{PREFIX}-sibterm-a", f"{PREFIX}-sibterm-b",
            f"{PREFIX}-sibterm-c", f"{PREFIX}-sibterm-d",
        )
        await _node(conn, node_a, repo_a, gid, "feat/x", "IN_PROGRESS")
        await _node(conn, node_b, repo_b, gid, "feat/x", "IN_PROGRESS")
        await _node(conn, node_c, repo_c, gid, "feat/x", "MERGED")     # already landed
        await _node(conn, node_d, repo_d, gid, "feat/x", "ESCALATED")  # stuck, needs a human
        group = f"{PREFIX}-sibterm-group"
        await _group(conn, group, gid, [node_a, node_b, node_c, node_d])
        assignee = f"{PREFIX}-user"
        await _merge_story(
            conn, f"{PREFIX}-sibterm-merge", gid, group, f"{PREFIX}-sibterm-thread", assignee,
        )
        await _branch_resource(conn, gid, "acme", "svc-a", "feat/x")
        br_b = await _branch_resource(conn, gid, "acme", "svc-b", "feat/x")
        br_c = await _branch_resource(conn, gid, "acme", "svc-c", "feat/x")
        br_d = await _branch_resource(conn, gid, "acme", "svc-d", "feat/x")

        h = {
            "node_id": node_a, "goal_id": gid, "owner": "acme", "name": "svc-a",
            "branch_name": "feat/x", "trunk_branch": "main", "git_repo_id": repo_a,
        }
        with patch("app.services.git_graph.grind.get_pool", lambda: _PoolShim(conn)):
            created = await _ensure_diff_story(h, "trunk-sha-1")
        assert created is True
        sid = await conn.fetchval(
            "SELECT id FROM stories WHERE git_node_id = $1 AND created_by = 'engine'",
            node_a,
        )

        granted_ids = {
            g["resource_id"] for g in await conn.fetch(
                "SELECT resource_id FROM story_resources WHERE story_id = $1", sid,
            )
        }
        assert br_b in granted_ids, "the LIVE sibling must still be granted"
        assert br_c not in granted_ids, (
            "the already-MERGED sibling must NOT be granted — nothing left there "
            "for the assistant to safely touch"
        )
        assert br_d not in granted_ids, (
            "the ESCALATED sibling must NOT be granted — it needs a human Retry, "
            "not a new commit from this session"
        )

        brief = await conn.fetchval("SELECT description FROM stories WHERE id = $1", sid)
        assert "svc-b" in brief, "the live sibling must still be mentioned in the brief"
        assert "svc-c" not in brief, (
            "the already-merged sibling must not be mentioned — telling the "
            "assistant to go check it would point it at a dead workstream"
        )
        assert "svc-d" not in brief, (
            "the escalated sibling must not be mentioned either — same reason"
        )


@pytest.mark.asyncio
async def test_ripple_sibling_grant_never_resolves_as_primary_repo():
    """Regression for the third instance of the primary-repo-tiebreak bug
    (2026-08-20): node A's Sync story is granted its OWN repo ('produces') and
    sibling node B's repo ('modifies') in the SAME transaction, so a raw
    created_at ASC tiebreak can't tell them apart. _resolve_repo_for_story and
    resolve_repos_for_story must both still resolve svc-a (the story's own
    repo, the one it's linked to via git_node_id) as primary — never svc-b —
    regardless of which grant happened to be inserted first.

    This is exactly the live failure mode that was found via `gh` history:
    two Integrate stories tied to a agentos git_node had their plan/scope
    files committed into agent_os_openclaw's git tree instead."""
    async with _conn() as conn:
        gid = f"{PREFIX}-sib2-goal"
        await _setup_goal(conn, gid)
        repo_a, repo_b = await _repo(conn, "svc-a"), await _repo(conn, "svc-b")
        node_a, node_b = f"{PREFIX}-sib2-a", f"{PREFIX}-sib2-b"
        await _node(conn, node_a, repo_a, gid, "feat/x", "IN_PROGRESS")
        await _node(conn, node_b, repo_b, gid, "feat/x", "IN_PROGRESS")
        group = f"{PREFIX}-sib2-group"
        await _group(conn, group, gid, [node_a, node_b])
        assignee = f"{PREFIX}-user"
        await _merge_story(conn, f"{PREFIX}-sib2-merge", gid, group, f"{PREFIX}-sib2-thread", assignee)
        await _branch_resource(conn, gid, "acme", "svc-a", "feat/x")
        await _branch_resource(conn, gid, "acme", "svc-b", "feat/x")

        h = {
            "node_id": node_a, "goal_id": gid, "owner": "acme", "name": "svc-a",
            "branch_name": "feat/x", "trunk_branch": "main", "git_repo_id": repo_a,
        }
        with patch("app.services.git_graph.grind.get_pool", lambda: _PoolShim(conn)):
            created = await _ensure_diff_story(h, "trunk-sha-1")
        assert created is True
        sid = await conn.fetchval(
            "SELECT id FROM stories WHERE git_node_id = $1 AND created_by = 'engine'",
            node_a,
        )

        from app.services.operator_provisioning import (
            _resolve_repo_for_story, resolve_repos_for_story,
        )

        primary = await _resolve_repo_for_story(conn, gid, sid)
        assert primary is not None
        assert primary["repo_name"] == "svc-a", (
            f"primary resolved to {primary['repo_name']!r}, not svc-a — the "
            "sibling ripple grant outranked the story's own repo"
        )

        resolved_all = await resolve_repos_for_story(conn, gid, sid)
        assert resolved_all, "expected both svc-a and svc-b to be resolved"
        assert resolved_all[0]["repo_name"] == "svc-a", (
            f"element 0 was {resolved_all[0]['repo_name']!r}, not svc-a — "
            "resolve_repos_for_story disagrees with _resolve_repo_for_story "
            "about which repo is primary"
        )


@pytest.mark.asyncio
async def test_single_repo_story_gets_no_sibling_grant():
    """A single-node group (the ordinary, non-multi-write case) must stay
    byte-identical — no sibling grant, no ripple-check paragraph in the brief."""
    async with _conn() as conn:
        gid = f"{PREFIX}-solo-goal"
        await _setup_goal(conn, gid)
        repo_a = await _repo(conn, "svc-a")
        node_a = f"{PREFIX}-solo-a"
        await _node(conn, node_a, repo_a, gid, "feat/x", "IN_PROGRESS")
        group = f"{PREFIX}-solo-group"
        await _group(conn, group, gid, [node_a])
        assignee = f"{PREFIX}-user"
        await _merge_story(conn, f"{PREFIX}-solo-merge", gid, group, f"{PREFIX}-solo-thread", assignee)
        await _branch_resource(conn, gid, "acme", "svc-a", "feat/x")

        h = {
            "node_id": node_a, "goal_id": gid, "owner": "acme", "name": "svc-a",
            "branch_name": "feat/x", "trunk_branch": "main", "git_repo_id": repo_a,
        }
        with patch("app.services.git_graph.grind.get_pool", lambda: _PoolShim(conn)):
            created = await _ensure_diff_story(h, "trunk-sha-1")

        assert created is True
        sid = await conn.fetchval(
            "SELECT id FROM stories WHERE git_node_id = $1 AND created_by = 'engine'",
            node_a,
        )
        granted = await conn.fetch(
            "SELECT resource_id FROM story_resources WHERE story_id = $1", sid,
        )
        assert len(granted) == 1, "single-node group must get exactly its own grant, nothing extra"

        brief = await conn.fetchval("SELECT description FROM stories WHERE id = $1", sid)
        assert "MULTI-REPO" not in brief
