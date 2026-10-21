"""`token_refresh.py` must scope a story's GitHub token to EVERY repo it holds, not
just one — against a real database.

The bug this pins: `refresh_story_token` and `_refresh_one_story` used to resolve a
story's repo via a `LEFT JOIN ... LIMIT 1` (or, in the sweep, a JOIN with no LIMIT at
all — which fanned a multi-repo story out into one row per repo and refreshed each in
turn, each call overwriting the last, so only the LAST-processed repo's token
survived). Either way a two-repo story ends up with a token scoped to ONE repo, and
the workspace's single shared credential helper silently loses write access to every
other repo — this is what produced "remote: Repository not found" pushing to a repo
that was granted, cloned, and previously working fine.

Both code paths now delegate repo resolution to
`operator_provisioning.resolve_repos_for_story` — the same function
`provision_story_workspace` uses for the initial clone — so these tests exercise that
shared resolution against real fixture rows and assert the token mint receives every
repo name, not just one.

GitHub (`generate_installation_token`) and the gateway (`gateway_admin.call`) are
mocked — this is a unit boundary these tests do not own — but the SQL that decides
WHICH repos go into that call runs against real Postgres.
"""
from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from app.services import token_refresh
from app.services.tests.test_local_workspace_nodes_sql import _story
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


@contextlib.asynccontextmanager
async def _patched(conn, *, mint_result: dict | None = None):
    """Route both `get_pool()` call sites (this module and `operator_provisioning`,
    which it delegates to) through the test's transaction-scoped connection, force
    `multi_repo_enabled` on, and stub out GitHub + the gateway."""
    from app import config as config_module
    from app.services import operator_provisioning

    settings = config_module.get_settings()
    mint = AsyncMock(return_value=mint_result or {"token": "ghs_test_token"})
    gw_call = AsyncMock(return_value={})
    with patch.object(token_refresh, "get_pool", lambda: conn), \
         patch.object(operator_provisioning, "get_settings", lambda: _FlagOn(settings)), \
         patch.object(token_refresh, "generate_installation_token", mint), \
         patch.object(token_refresh, "gateway_admin") as gw:
        gw.call = gw_call
        yield mint, gw_call


@pytest.mark.asyncio
async def test_on_demand_refresh_scopes_the_token_to_both_repos():
    async with _conn() as conn:
        sid = f"{PREFIX}-refresh-ondemand"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute(
            "UPDATE stories SET shca_instance_id = $1 WHERE id = $2",
            f"{PREFIX}-instance", sid,
        )
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "repo-b", relation="produces",
                     is_write=True, branch_name="feat/x")

        async with _patched(conn) as (mint, gw_call):
            ok, err = await token_refresh.refresh_story_token(sid)

        assert ok is True, err
        minted_repos = mint.call_args[0][0]
        assert set(minted_repos) == {"repo-a", "repo-b"}
        assert gw_call.call_args[0][1]["githubToken"] == "ghs_test_token"


@pytest.mark.asyncio
async def test_background_sweep_scopes_the_token_to_both_repos():
    async with _conn() as conn:
        sid = f"{PREFIX}-refresh-sweep"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute(
            "UPDATE stories SET shca_instance_id = $1 WHERE id = $2",
            f"{PREFIX}-instance", sid,
        )
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "repo-b", relation="produces",
                     is_write=True, branch_name="feat/x")

        async with _patched(conn) as (mint, gw_call):
            await token_refresh._refresh_one_story({
                "id": sid, "goal_id": f"{sid}-goal", "shca_instance_id": f"{PREFIX}-instance",
            })

        assert mint.await_count == 1, "must mint exactly ONE token covering both repos"
        minted_repos = mint.call_args[0][0]
        assert set(minted_repos) == {"repo-a", "repo-b"}


@pytest.mark.asyncio
async def test_a_single_repo_story_is_unaffected():
    async with _conn() as conn:
        sid = f"{PREFIX}-refresh-single"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute(
            "UPDATE stories SET shca_instance_id = $1 WHERE id = $2",
            f"{PREFIX}-instance", sid,
        )
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")

        async with _patched(conn) as (mint, gw_call):
            ok, err = await token_refresh.refresh_story_token(sid)

        assert ok is True, err
        assert mint.call_args[0][0] == ["repo-a"]


@pytest.mark.asyncio
async def test_a_story_with_no_repo_resource_is_refused():
    async with _conn() as conn:
        sid = f"{PREFIX}-refresh-norepo"
        await _story(conn, sid, execution_mode="operator")
        await conn.execute(
            "UPDATE stories SET shca_instance_id = $1 WHERE id = $2",
            f"{PREFIX}-instance", sid,
        )

        async with _patched(conn) as (mint, gw_call):
            ok, err = await token_refresh.refresh_story_token(sid)

        assert ok is False
        assert "no repo resource" in err
        mint.assert_not_awaited()
