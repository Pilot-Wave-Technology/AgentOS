"""N-repo local execution — control-plane side (research/NREPO_LOCAL_EXECUTION_PLAN.md,
C2/C7/C9), against a real database.

Covers the genuinely new SQL-backed logic: `local_hydrate._resolve_live_repos` (the
plural sibling of `_repo_ref`), `_multi_repo_incomplete` (the local analog of
`operator_provisioning.is_write_repo_set_incomplete`), `local_workspace_nodes.
record_local_workspace_node`'s multi-repo path, and `local_checkout._import_readiness`'s
capability gate. `mint_repo_clone_token`/`mint_push_token`/`submit_local_diff` are
exercised at the unit level elsewhere where GitHub calls need mocking; this file stays
to what real Postgres can answer.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

import pytest

from app.services.git_graph.repo_identity import canonical_repo_id
from app.services.local_checkout import _import_readiness
from app.services.local_hydrate import _multi_repo_incomplete, _resolve_live_repos
from app.services.local_workspace_nodes import record_local_workspace_node
from app.services.tests.test_local_workspace_nodes_sql import _PoolShim, _story
from app.services.tests.test_multi_repo_freshness_fix import _grant
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


async def _git_node(
    conn, sid: str, owner: str, name: str, branch: str, *,
    state: str = "IN_PROGRESS", created_at: datetime | None = None,
    updated_at: datetime | None = None,
):
    """A real `git_repos` + `git_nodes` + `git_node_stories` row set — what
    `_resolve_live_repos` actually reads. Distinct from `story_workspace_nodes`
    (what gets RECORDED once resolved) and from `goal_resources`/`story_resources`
    (what gets GRANTED) — three different tables answering three different
    questions, all exercised in this file."""
    repo_id = canonical_repo_id(owner, name)
    await conn.execute(
        "INSERT INTO git_repos (id, owner, name, clone_url) VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (id) DO NOTHING",
        repo_id, owner, name, f"https://github.com/{owner}/{name}.git",
    )
    node_id = f"{sid}-{name}-{branch.replace('/', '-')}-node"
    created_at = created_at or datetime.now(timezone.utc)
    updated_at = updated_at or created_at
    await conn.execute(
        """INSERT INTO git_nodes
               (id, git_repo_id, goal_id, branch_name, state, trunk_branch,
                created_at, updated_at)
           VALUES ($1,$2,$3,$4,$5,'main',$6,$7)
           ON CONFLICT (id) DO UPDATE SET
               state = EXCLUDED.state, updated_at = EXCLUDED.updated_at""",
        node_id, repo_id, f"{sid}-goal", branch, state, created_at, updated_at,
    )
    await conn.execute(
        "INSERT INTO git_node_stories (git_node_id, story_id) VALUES ($1,$2) "
        "ON CONFLICT (git_node_id, story_id) DO NOTHING",
        node_id, sid,
    )


# ── _resolve_live_repos ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolves_every_distinct_repo():
    async with _conn() as conn:
        sid = f"{PREFIX}-live-a"
        await _story(conn, sid, execution_mode="local")
        await _git_node(conn, sid, "acme", "svc-a", "feat/x")
        await _git_node(conn, sid, "acme", "svc-b", "feat/x")

        repos = await _resolve_live_repos(conn, sid)
        assert {(r["owner"], r["repo"]) for r in repos} == {("acme", "svc-a"), ("acme", "svc-b")}


@pytest.mark.asyncio
async def test_orders_by_created_at_not_updated_at():
    """Entry 0 must stay the FIRST-CUT repo even after a later repo's branch gets
    pushed to (updated_at bumped) — every consumer (materializeAll, pushAll's
    primary/extras split) trusts entry 0 as stable for the life of the checkout."""
    async with _conn() as conn:
        sid = f"{PREFIX}-live-order"
        await _story(conn, sid, execution_mode="local")
        t0 = datetime.now(timezone.utc)
        await _git_node(conn, sid, "acme", "first-cut", "feat/x", created_at=t0)
        await _git_node(
            conn, sid, "acme", "second-cut", "feat/x",
            created_at=t0 + timedelta(seconds=5),
            updated_at=t0 + timedelta(minutes=10),  # pushed to LONG after first-cut
        )

        repos = await _resolve_live_repos(conn, sid)
        assert [r["repo"] for r in repos] == ["first-cut", "second-cut"], (
            "the more-recently-updated repo must not jump to entry 0"
        )


@pytest.mark.asyncio
async def test_several_live_nodes_in_one_repo_collapse_to_the_current_one():
    async with _conn() as conn:
        sid = f"{PREFIX}-live-recut"
        await _story(conn, sid, execution_mode="local")
        t0 = datetime.now(timezone.utc)
        await _git_node(
            conn, sid, "acme", "svc-a", "feat/old",
            created_at=t0, updated_at=t0,
        )
        await _git_node(
            conn, sid, "acme", "svc-a", "feat/new",
            created_at=t0 + timedelta(seconds=1), updated_at=t0 + timedelta(minutes=5),
        )

        repos = await _resolve_live_repos(conn, sid)
        assert len(repos) == 1, "a re-cut in the SAME repo must not look like two repos"
        assert repos[0]["branch"] == "feat/new", "the current (most recently updated) node wins"


@pytest.mark.asyncio
async def test_no_live_nodes_is_an_empty_list_not_an_error():
    async with _conn() as conn:
        sid = f"{PREFIX}-live-none"
        await _story(conn, sid, execution_mode="local")
        assert await _resolve_live_repos(conn, sid) == []


# ── _multi_repo_incomplete ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_incomplete_when_granted_exceeds_live():
    async with _conn() as conn:
        sid = f"{PREFIX}-incomplete-a"
        await _story(conn, sid, execution_mode="local")
        await _grant(conn, sid, "acme", "svc-a", relation="produces", is_write=True)
        await _grant(conn, sid, "acme", "svc-b", relation="produces", is_write=True)
        await _git_node(conn, sid, "acme", "svc-a", "feat/x")
        # svc-b: granted, but its branch hasn't cut yet.

        assert await _multi_repo_incomplete(conn, sid, live_count=1) is True


@pytest.mark.asyncio
async def test_complete_when_granted_equals_live():
    async with _conn() as conn:
        sid = f"{PREFIX}-complete-a"
        await _story(conn, sid, execution_mode="local")
        await _grant(conn, sid, "acme", "svc-a", relation="produces", is_write=True)
        await _grant(conn, sid, "acme", "svc-b", relation="produces", is_write=True)
        await _git_node(conn, sid, "acme", "svc-a", "feat/x")
        await _git_node(conn, sid, "acme", "svc-b", "feat/x")

        assert await _multi_repo_incomplete(conn, sid, live_count=2) is False


# ── record_local_workspace_node: multi-repo ──────────────────────────────────

@pytest.mark.asyncio
async def test_multi_repo_records_one_row_per_repo_under_repos_layout():
    async with _conn() as conn:
        sid = f"{PREFIX}-record-multi"
        await _story(conn, sid, execution_mode="local")
        await _git_node(conn, sid, "acme", "svc-a", "feat/x")
        await _git_node(conn, sid, "acme", "svc-b", "feat/x")

        recorded = await record_local_workspace_node(_PoolShim(conn), sid, multi_repo=True)
        assert recorded is True

        rows = await conn.fetch(
            "SELECT repo_name, subdir FROM story_workspace_nodes WHERE story_id = $1 "
            "ORDER BY repo_name", sid,
        )
        subdirs = {r["repo_name"]: r["subdir"] for r in rows}
        assert subdirs == {
            "svc-a": "repos/acme__svc-a",
            "svc-b": "repos/acme__svc-b",
        }, "multi-repo must never use the legacy single 'repo' directory for either"


@pytest.mark.asyncio
async def test_multi_repo_never_moves_a_repo_already_on_disk():
    async with _conn() as conn:
        sid = f"{PREFIX}-record-stable"
        await _story(conn, sid, execution_mode="local")
        await _git_node(conn, sid, "acme", "svc-a", "feat/x")
        pool = _PoolShim(conn)
        await record_local_workspace_node(pool, sid, multi_repo=False)  # single-repo, legacy dir

        # Now the story ALSO gets a second live repo — the multi-repo path takes over.
        await _git_node(conn, sid, "acme", "svc-b", "feat/x")
        await record_local_workspace_node(pool, sid, multi_repo=True)

        rows = await conn.fetch(
            "SELECT repo_name, subdir FROM story_workspace_nodes WHERE story_id = $1 "
            "ORDER BY repo_name", sid,
        )
        subdirs = {r["repo_name"]: r["subdir"] for r in rows}
        assert subdirs["svc-a"] == "repo", (
            "svc-a was already cloned into the legacy dir — recording it under "
            "repos/acme__svc-a now would orphan whatever is on disk"
        )
        assert subdirs["svc-b"] == "repos/acme__svc-b", "a brand-new repo gets the new layout"


# ── _import_readiness: the capability gate ───────────────────────────────────

async def _accepted_story_with_two_repos(conn, sid: str):
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
        f"{sid}-goal", f"fixture {sid}", f"{PREFIX}-user",
    )
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, created_by, execution_mode, state) "
        "VALUES ($1,$2,$3,$4,'operator','accepted')",
        sid, f"{sid}-goal", "fixture", f"{PREFIX}-user",
    )
    await _grant(conn, sid, "acme", "svc-a", relation="produces", is_write=True)
    await _grant(conn, sid, "acme", "svc-b", relation="produces", is_write=True)


@pytest.mark.asyncio
async def test_import_refused_for_two_repos_without_the_capability():
    async with _conn() as conn:
        sid = f"{PREFIX}-readiness-no-cap"
        await _accepted_story_with_two_repos(conn, sid)

        refusal = await _import_readiness(conn, sid, f"{sid}-goal", multi_repo=False)
        assert refusal is not None
        assert refusal[0] == "multi_repo_unsupported"


@pytest.mark.asyncio
async def test_import_allowed_for_two_repos_with_the_capability():
    async with _conn() as conn:
        sid = f"{PREFIX}-readiness-cap"
        await _accepted_story_with_two_repos(conn, sid)

        refusal = await _import_readiness(conn, sid, f"{sid}-goal", multi_repo=True)
        assert refusal is None


# ── #25 regression: a read-only ("needs") repo never blocks the checkout ──────
#
# Fixed on 2026-08-25 (171368aa) and LOST: the refactor line never carried it, so on
# feature `_multi_repo_incomplete` counted every granted repo -- `needs` included --
# against live branches, which only write repos ever get. A story granted one
# `modifies` repo and two `needs` repos computed 3 > 1 and could never be checked
# out locally; the extension said "the branch isn't cut yet" for ever.

@pytest.mark.asyncio
async def test_needs_repos_do_not_make_a_cut_story_look_incomplete():
    async with _conn() as conn:
        sid = f"{PREFIX}-needs-a"
        await _story(conn, sid, execution_mode="local")
        await _grant(conn, sid, "acme", "svc-a", relation="modifies", is_write=True)
        await _grant(conn, sid, "acme", "ctx-1", relation="needs", is_write=False)
        await _grant(conn, sid, "acme", "ctx-2", relation="needs", is_write=False)
        await _git_node(conn, sid, "acme", "svc-a", "feat/x")

        assert await _multi_repo_incomplete(conn, sid, live_count=1) is False


@pytest.mark.asyncio
async def test_a_write_repo_still_being_cut_is_still_incomplete_beside_needs():
    async with _conn() as conn:
        sid = f"{PREFIX}-needs-b"
        await _story(conn, sid, execution_mode="local")
        await _grant(conn, sid, "acme", "svc-a", relation="modifies", is_write=True)
        await _grant(conn, sid, "acme", "svc-b", relation="produces", is_write=True)
        await _grant(conn, sid, "acme", "ctx-1", relation="needs", is_write=False)
        await _git_node(conn, sid, "acme", "svc-a", "feat/x")

        assert await _multi_repo_incomplete(conn, sid, live_count=1) is True
