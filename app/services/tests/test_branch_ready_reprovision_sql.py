"""N-repo local execution's OPERATOR-lane counterpart bug (research/
MULTI_REPO_STORIES_PLAN.md §12.5, pieces 1b/1c), found via real end-to-end testing
2026-08-19 on story `450a89cb`: `provision_story_workspace` deliberately clones only
the write repos with a live branch RIGHT NOW and still returns `ok=True` — a
legitimate, common single-repo outcome. For a multi-repo story that means
`dispatch_story_kickoff_to_assistant` could mark the workspace 'ready' and dispatch
kickoff in the SAME call, with a repo still missing (piece 1b, in assistant.py).
Deferring kickoff there is only useful if something re-checks later — and
`BranchReadyListener._reprovision_story`'s own guard (`if workspace_state == 'ready':
return`) meant NOTHING ever would: once "ready", Component C considered its job done,
permanently. Piece 1c closes that: the early return now also checks completeness.

This file exercises piece 1c specifically — the piece with a real database
guard-logic and no existing test coverage — against real Postgres. Piece 1b lives in
a large router function (`dispatch_story_kickoff_to_assistant`) not practically unit-
testable without extensive gateway mocking; its logic is the same already-tested
`is_write_repo_set_incomplete` predicate as piece 1c, just gating a `return False`
instead of an early skip — verified by direct code reading, not a new test.
"""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from app.services.git_graph.branch_ready_listener import BranchReadyListener
from app.services.tests.test_local_workspace_nodes_sql import _node, _story
from app.services.tests.test_multi_repo_freshness_fix import _FlagOn, _grant
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


class _PoolShim:
    """`_reprovision_story` does `conn = await pool.acquire()` directly (no context
    manager) and never releases it explicitly (relies on GC) — a plain connection
    with `.acquire()` returning itself, matching the pattern other SQL tests in this
    suite use for functions written against the real pool interface."""

    def __init__(self, conn):
        self._conn = conn

    async def acquire(self):
        return self._conn

    def __getattr__(self, name):
        return getattr(self._conn, name)


async def _ready_operator_story(conn, sid: str, *, agent_id: str) -> None:
    """A story already imported to an SHCA (thread + agent), operator mode,
    in_progress, workspace already 'ready' — the exact state the original bug left
    story 450a89cb in before kickoff should have been (but, pre-fix, already was)
    dispatched."""
    await conn.execute(
        "INSERT INTO agents (id, name, created_by) VALUES ($1, $1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        agent_id, f"{PREFIX}-user",
    )
    thread_id = f"{sid}-thread"
    await conn.execute(
        "INSERT INTO threads (id, goal_id, agent_id, created_by) VALUES ($1,$2,$3,$4)",
        thread_id, f"{sid}-goal", agent_id, f"{PREFIX}-user",
    )
    await conn.execute(
        "UPDATE stories SET execution_mode='operator', state='in_progress', "
        "workspace_state='ready', active_thread_id=$2, assigned_to=$3, "
        "story_kind='code', created_by=$3 WHERE id=$1",
        sid, thread_id, f"{PREFIX}-user",
    )


@pytest.mark.asyncio
async def test_ready_but_incomplete_workspace_still_gets_reprovisioned():
    """The exact defect: a ready workspace missing a repo whose branch just cut must
    NOT be treated as "nothing to do" — kickoff has to get a chance to run (deferred
    by piece 1b, or genuinely dispatched if this is its first real chance)."""
    async with _conn() as conn:
        sid = f"{PREFIX}-reprov-incomplete"
        agent_id = f"{PREFIX}-agent-a"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "svc-a", relation="produces", is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "svc-b", relation="produces", is_write=True, branch_name="feat/x")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        # svc-b: granted, but not yet recorded — the repo still missing.
        await _ready_operator_story(conn, sid, agent_id=agent_id)

        listener = BranchReadyListener()
        dispatch_mock = AsyncMock(return_value=True)
        with patch("app.services.git_graph.branch_ready_listener.get_pool", lambda: _PoolShim(conn)), \
             patch("app.services.operator_provisioning.get_settings",
                   lambda: _FlagOn(__import__("app.config", fromlist=["get_settings"]).get_settings())), \
             patch("app.routers.assistant.dispatch_story_kickoff_to_assistant", dispatch_mock):
            await listener._reprovision_story(f"{sid}-goal", sid)

        dispatch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_and_complete_workspace_is_left_alone():
    """The existing, correct behaviour for the common case must survive unchanged:
    once every granted repo is actually recorded, this listener has nothing to do."""
    async with _conn() as conn:
        sid = f"{PREFIX}-reprov-complete"
        agent_id = f"{PREFIX}-agent-b"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "svc-a", relation="produces", is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "svc-b", relation="produces", is_write=True, branch_name="feat/x")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        await _node(conn, sid, "acme", "svc-b", "feat/x", "repos/acme__svc-b")
        await _ready_operator_story(conn, sid, agent_id=agent_id)

        listener = BranchReadyListener()
        dispatch_mock = AsyncMock(return_value=True)
        with patch("app.services.git_graph.branch_ready_listener.get_pool", lambda: _PoolShim(conn)), \
             patch("app.services.operator_provisioning.get_settings",
                   lambda: _FlagOn(__import__("app.config", fromlist=["get_settings"]).get_settings())), \
             patch("app.routers.assistant.dispatch_story_kickoff_to_assistant", dispatch_mock):
            await listener._reprovision_story(f"{sid}-goal", sid)

        dispatch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_repo_story_is_unaffected_multi_repo_flag_off():
    """Byte-for-byte unchanged behaviour for the single-repo case, capability flag at
    its real (off) default — `is_write_repo_set_incomplete` short-circuits False
    without even querying, so a single-repo story's ready workspace is still treated
    as genuinely done, exactly as before piece 1c existed."""
    async with _conn() as conn:
        sid = f"{PREFIX}-reprov-single"
        agent_id = f"{PREFIX}-agent-c"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "svc-a", relation="produces", is_write=True, branch_name="feat/x")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repo")
        await _ready_operator_story(conn, sid, agent_id=agent_id)

        listener = BranchReadyListener()
        dispatch_mock = AsyncMock(return_value=True)
        with patch("app.services.git_graph.branch_ready_listener.get_pool", lambda: _PoolShim(conn)), \
             patch("app.routers.assistant.dispatch_story_kickoff_to_assistant", dispatch_mock):
            await listener._reprovision_story(f"{sid}-goal", sid)

        dispatch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reprovision_retries_when_lock_briefly_held():
    """2026-08-20 fix: a multi-write story's member repos get their branches cut a
    few seconds apart — each cut fires its own NOTIFY for the SAME story, and the
    2nd/3rd can land while the 1st cut's re-provision is still holding the per-story
    advisory lock. The try-lock failure path used to just `return` with zero log line,
    permanently dropping that NOTIFY (confirmed live on story e77e0b65: 2 of 3 write
    repos silently lost forever). It must now retry instead, and succeed once the
    lock frees rather than giving up on the first contended attempt."""
    async with _conn() as conn:
        sid = f"{PREFIX}-reprov-retry"
        agent_id = f"{PREFIX}-agent-retry"
        await _story(conn, sid, execution_mode="operator")
        thread_id = f"{sid}-thread"
        await conn.execute(
            "INSERT INTO agents (id, name, created_by) VALUES ($1, $1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            agent_id, f"{PREFIX}-user",
        )
        await conn.execute(
            "INSERT INTO threads (id, goal_id, agent_id, created_by) VALUES ($1,$2,$3,$4)",
            thread_id, f"{sid}-goal", agent_id, f"{PREFIX}-user",
        )
        # Deliberately NOT 'ready': this test is only about the lock-retry mechanism,
        # not the incomplete-write-repo-set logic (covered by the tests above) — a
        # not-yet-ready operator story with a thread+agent reaches the unconditional
        # dispatch call at the bottom of _reprovision_story.
        await conn.execute(
            "UPDATE stories SET execution_mode='operator', state='in_progress', "
            "workspace_state='none', active_thread_id=$2 WHERE id=$1",
            sid, thread_id,
        )

        holder = await _connect_or_skip()
        try:
            lock_key = f"branch-ready:{sid}"
            got = await holder.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", lock_key)
            assert got, "test setup: could not take the lock to simulate contention"

            async def _release_soon():
                await asyncio.sleep(0.3)
                await holder.execute(
                    "SELECT pg_advisory_unlock(hashtext($1)::bigint)", lock_key)

            release_task = asyncio.create_task(_release_soon())
            listener = BranchReadyListener()
            dispatch_mock = AsyncMock(return_value=True)
            with patch("app.services.git_graph.branch_ready_listener.get_pool",
                       lambda: _PoolShim(conn)), \
                 patch("app.services.git_graph.branch_ready_listener._LOCK_RETRY_DELAY_S", 0.1), \
                 patch("app.routers.assistant.dispatch_story_kickoff_to_assistant", dispatch_mock):
                await listener._reprovision_story(f"{sid}-goal", sid)
            await release_task
            dispatch_mock.assert_awaited_once()
        finally:
            await holder.close()


@pytest.mark.asyncio
async def test_poll_backstop_catches_ready_but_incomplete_story():
    """2026-08-20 fix: the poll backstop previously only looked at
    workspace_state='none' — a 'ready'-but-incomplete story (the state a dropped
    NOTIFY leaves it in) had no backstop at all. This sweep must find it and retry."""
    async with _conn() as conn:
        sid = f"{PREFIX}-scan-incomplete"
        agent_id = f"{PREFIX}-agent-scan"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "svc-a", relation="produces", is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "svc-b", relation="produces", is_write=True, branch_name="feat/x")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        # svc-b: granted, but not yet recorded — the repo still missing.
        await _ready_operator_story(conn, sid, agent_id=agent_id)

        listener = BranchReadyListener()
        reprov_mock = AsyncMock(return_value=None)
        with patch("app.services.git_graph.branch_ready_listener.get_pool", lambda: _PoolShim(conn)), \
             patch("app.services.operator_provisioning.get_settings",
                   lambda: _FlagOn(__import__("app.config", fromlist=["get_settings"]).get_settings())), \
             patch.object(listener, "_reprovision_story", reprov_mock):
            await listener._scan()

        reprov_mock.assert_any_await(f"{sid}-goal", sid)
