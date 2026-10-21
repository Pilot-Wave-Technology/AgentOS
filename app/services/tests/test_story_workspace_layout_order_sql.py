"""The 2026-08-20 primary-repo-flip incident, against a real database.

Story `1b54fd23`'s first checkout recorded `agentos` as primary. Its reimport
later disagreed — `resolve_workspace_clones` re-derived "primary" by sorting
(first `subdir ASC`, then `created_at ASC`), and BOTH disagreed with the original
checkout, because a multi-write story's repos are created in one batched
transaction and tie on `created_at` down to the microsecond: no sort key was ever
going to be stable across two separate provisioning calls. Two `.agentos/bridge.json`
files resulted, disagreeing about which repo the plan directory is in.

The actual fix: `story_workspace_nodes.is_primary` is decided ONCE (the first
provisioning call for a story) and preserved on every later call regardless of what
order the caller's `clones` arrive in — `_record_story_workspace_nodes` reads the
existing decision before its delete-then-reinsert and re-applies it.

These tests call the REAL production write path (`_record_story_workspace_nodes`)
and then `resolve_workspace_clones`, against real Postgres, not a mock — the whole
point is verifying persistence survives a reimport whose input order differs from
the original, which is exactly what the incident did.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.operator_provisioning import _record_story_workspace_nodes
from app.services.story_workspace_layout import resolve_workspace_clones
from app.services.tests.test_local_workspace_nodes_sql import _PoolShim
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


async def _story(conn, sid: str) -> None:
    gid = f"{sid}-goal"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1, $2, $3)",
        gid, f"fixture {gid}", f"{PREFIX}-user",
    )
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by, story_kind) "
        "VALUES ($1, $2, 'fixture', 'in_progress', $3, 'code')",
        sid, gid, f"{PREFIX}-user",
    )


def _clone(owner: str, name: str, subdir: str) -> dict:
    return {
        "git_repo_id": f"gr_{name}", "branch_name": "feat/x", "subdir": subdir,
        "is_write": True, "repo_owner": owner, "repo_name": name,
    }


_PWT = _clone("Pilot-Wave-Technology", "agentos", "repos/Pilot-Wave-Technology__agentos")
_OPENCLAW = _clone("Pilot-Wave-Technology", "agent_os_openclaw", "repos/Pilot-Wave-Technology__agent_os_openclaw")


@pytest.mark.asyncio
async def test_first_provisioning_call_decides_primary_from_input_order():
    """The first-ever call for a story has no existing decision to preserve — it
    decides fresh, from the caller's own clones order (index 0)."""
    async with _conn() as conn:
        sid = f"{PREFIX}-order-a"
        await _story(conn, sid)
        await _record_story_workspace_nodes(_PoolShim(conn), sid, [_PWT, _OPENCLAW])

        resolved = await resolve_workspace_clones(conn, sid)

        assert [c["repo_name"] for c in resolved] == ["agentos", "agent_os_openclaw"]


@pytest.mark.asyncio
async def test_reimport_with_reversed_input_order_does_not_flip_primary():
    """THE actual incident, reproduced directly: the first call decides agentos
    is primary. A reimport arrives with the clones in the OPPOSITE order (exactly
    what a differently-sorted upstream query would hand it) — primary must stay
    agentos, not flip to whichever repo is now first in the input."""
    async with _conn() as conn:
        sid = f"{PREFIX}-order-b"
        await _story(conn, sid)
        await _record_story_workspace_nodes(_PoolShim(conn), sid, [_PWT, _OPENCLAW])
        first = await resolve_workspace_clones(conn, sid)
        assert first[0]["repo_name"] == "agentos"

        # Reimport: same two repos, REVERSED input order.
        await _record_story_workspace_nodes(_PoolShim(conn), sid, [_OPENCLAW, _PWT])
        second = await resolve_workspace_clones(conn, sid)

        assert second[0]["repo_name"] == "agentos", (
            "a reimport whose input order differs from the original must NOT change "
            "which repo is primary — that's the exact bug this fix closes"
        )
        assert [c["repo_name"] for c in first] == [c["repo_name"] for c in second]


@pytest.mark.asyncio
async def test_primary_falls_back_fresh_when_the_old_primary_repo_is_gone():
    """If the previously-primary repo's grant was revoked (it's no longer among the
    new clones), the decision can't be preserved — must decide fresh rather than
    leaving the story with no primary recorded at all."""
    async with _conn() as conn:
        sid = f"{PREFIX}-order-c"
        await _story(conn, sid)
        await _record_story_workspace_nodes(_PoolShim(conn), sid, [_PWT, _OPENCLAW])
        assert (await resolve_workspace_clones(conn, sid))[0]["repo_name"] == "agentos"

        # agentos's grant is revoked — only agent_os_openclaw remains.
        await _record_story_workspace_nodes(_PoolShim(conn), sid, [_OPENCLAW])
        resolved = await resolve_workspace_clones(conn, sid)

        assert len(resolved) == 1
        assert resolved[0]["repo_name"] == "agent_os_openclaw"

        row = await conn.fetchval(
            "SELECT is_primary FROM story_workspace_nodes WHERE story_id = $1", sid,
        )
        assert row is True, "the sole remaining repo must be marked primary"
