"""Multi-repo first-execution fix, piece 2 (`research/MULTI_REPO_STORIES_PLAN.md`
§12.5), against a real database: `grant_resource` must refuse a NEW write-repo grant
once the story's kickoff has been sent.

This is the other half of the two-piece fix. Piece 1 (`test_multi_repo_freshness_fix.py`)
lets a pre-kickoff story safely redo a workspace missing a late-cut branch; piece 2
makes the post-kickoff case structurally unreachable by refusing the grant that would
create it in the first place. Both pieces key off the same `story_has_had_kickoff`
predicate, so this file constructs the same kind of DB state — a story with (or
without) the kickoff marker posted — rather than any timing.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services import resource_allocation
from app.services.tests.test_multi_repo_freshness_fix import (
    _FlagOn,
    _grant,
    _post_kickoff_marker,
)
from app.services.tests.test_local_workspace_nodes_sql import _story
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
        leaked = await conn.fetchval(
            "SELECT count(*) FROM stories WHERE id LIKE $1", f"{PREFIX}%",
        )
        await conn.close()
        assert leaked == 0, f"{leaked} fixture rows survived rollback — dev DB dirtied"


@contextlib.asynccontextmanager
async def _grant_resource_against(conn):
    """`grant_resource` resolves its own pool via `get_pool()` (in both
    `app.services.resource_allocation` and `app.database`, since `resolve_goal_id`
    calls it too) and its own settings via `get_settings()` — both must be patched to
    route through this test's transaction-scoped connection and forced-on flag."""
    from unittest.mock import patch
    from app import database as database_module
    from app.config import get_settings as real_get_settings

    with patch.object(resource_allocation, "get_pool", lambda: conn), \
         patch.object(database_module, "get_pool", lambda: conn), \
         patch.object(resource_allocation, "get_settings",
                      lambda: _FlagOn(real_get_settings())):
        yield


async def _mint_grantable_repo(conn, sid: str, owner: str, name: str):
    """A `goal_resources` row for a repo NOT yet granted to the story — the thing
    `grant_resource` is about to be asked to attach."""
    rid = f"{sid}-ungranted-{name}"
    await conn.execute(
        """INSERT INTO goal_resources (id, goal_id, kind, name, created_by, status,
                                       repo_owner, repo_name)
           VALUES ($1,$2,'repo',$3,$4,'active',$5,$6)""",
        rid, f"{sid}-goal", f"{name}-grant", f"{PREFIX}-user", owner, name,
    )
    return rid


@pytest.mark.asyncio
async def test_refuses_a_new_write_repo_once_kickoff_was_sent():
    async with _conn() as conn:
        sid = f"{PREFIX}-refuse"
        thread = f"{PREFIX}-refuse-thread"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute("UPDATE stories SET active_thread_id = $1 WHERE id = $2",
                            thread, sid)
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _post_kickoff_marker(conn, thread, sid)
        new_repo_id = await _mint_grantable_repo(conn, sid, "acme", "repo-b")

        async with _grant_resource_against(conn):
            ok, reason = await resource_allocation.grant_resource(
                f"{sid}-goal", "story", sid, new_repo_id, relation="produces",
            )
        assert ok is False
        assert "kickoff" in reason.lower()


@pytest.mark.asyncio
async def test_allows_the_same_new_write_repo_before_kickoff():
    async with _conn() as conn:
        sid = f"{PREFIX}-allow-prekickoff"
        thread = f"{PREFIX}-allow-prekickoff-thread"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute("UPDATE stories SET active_thread_id = $1 WHERE id = $2",
                            thread, sid)
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        # No kickoff marker posted this time.
        new_repo_id = await _mint_grantable_repo(conn, sid, "acme", "repo-b")

        async with _grant_resource_against(conn):
            ok, reason = await resource_allocation.grant_resource(
                f"{sid}-goal", "story", sid, new_repo_id, relation="produces",
            )
        assert ok is True, reason


@pytest.mark.asyncio
async def test_regranting_an_already_held_repo_is_allowed_even_after_kickoff():
    """The engine cutting the feature branch for a repo the story already holds (a
    second `goal_resources` row for the same owner/name) is not a NEW repo and must
    stay allowed regardless of kickoff."""
    async with _conn() as conn:
        sid = f"{PREFIX}-regrant"
        thread = f"{PREFIX}-regrant-thread"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute("UPDATE stories SET active_thread_id = $1 WHERE id = $2",
                            thread, sid)
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name=None)  # bare repo grant
        await _post_kickoff_marker(conn, thread, sid)
        # The engine's branch-cut grant for the SAME repo.
        branch_id = f"{sid}-repo-a-branch"
        await conn.execute(
            """INSERT INTO goal_resources (id, goal_id, kind, role, name, created_by,
                                           status, repo_owner, repo_name, branch_name)
               VALUES ($1,$2,'branch','feature',$3,$4,'active',$5,$6,$7)""",
            branch_id, f"{sid}-goal", "repo-a-feature", f"{PREFIX}-user",
            "acme", "repo-a", "feat/x",
        )

        async with _grant_resource_against(conn):
            ok, reason = await resource_allocation.grant_resource(
                f"{sid}-goal", "story", sid, branch_id, relation="produces",
            )
        assert ok is True, reason


@pytest.mark.asyncio
async def test_readonly_needs_grant_is_allowed_even_after_kickoff():
    async with _conn() as conn:
        sid = f"{PREFIX}-readonly-postkickoff"
        thread = f"{PREFIX}-readonly-postkickoff-thread"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute("UPDATE stories SET active_thread_id = $1 WHERE id = $2",
                            thread, sid)
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _post_kickoff_marker(conn, thread, sid)
        context_repo_id = await _mint_grantable_repo(conn, sid, "acme", "repo-ctx")

        async with _grant_resource_against(conn):
            ok, reason = await resource_allocation.grant_resource(
                f"{sid}-goal", "story", sid, context_repo_id, relation="needs",
            )
        assert ok is True, reason


@pytest.mark.asyncio
async def test_non_operator_story_is_unaffected_by_kickoff():
    """This fix's clone path is operator-mode only — a local-execution story's
    grant flow must not change."""
    async with _conn() as conn:
        sid = f"{PREFIX}-local-mode"
        thread = f"{PREFIX}-local-mode-thread"
        await _story(conn, sid, execution_mode="local")
        await conn.execute("UPDATE stories SET active_thread_id = $1 WHERE id = $2",
                            thread, sid)
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _post_kickoff_marker(conn, thread, sid)
        new_repo_id = await _mint_grantable_repo(conn, sid, "acme", "repo-b")

        async with _grant_resource_against(conn):
            ok, reason = await resource_allocation.grant_resource(
                f"{sid}-goal", "story", sid, new_repo_id, relation="produces",
            )
        assert ok is True, reason
