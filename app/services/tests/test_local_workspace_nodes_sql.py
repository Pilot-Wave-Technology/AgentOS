"""Local checkouts must record a `story_workspace_nodes` row — against a real DB.

Local stories previously had NO rows in that table, because the only writer is the
gateway provisioner and it refuses local stories. Consequences these tests pin:

  * `/internal/git-credential` raised **HTTP 404 "story ... has no provisioned repos"**
    for every local story (`git_credential._resolve_story_repo_scope`: `if not rows:
    raise HTTPException(404)`). That is the hard failure this fixes.
  * `diff_review._story_write_repo_count` read 0 instead of 1.

And the regression this must NOT cause: populating the table makes alias resolution
possible, which could set `tasks.repo_subdir` and have `build_dispatch_message` emit
"Repository: `repo/` — cd repo/ ...". The extension opens the clone directory itself as
the workspace folder, so the agent's cwd is ALREADY inside `repo` — that instruction
would be wrong. `dag_ingest` skips alias resolution for local stories; the last two
tests pin that the skip exists AND that it is targeted at local stories only.

Runs against the local-development stack's PostgreSQL (port 5433) and skips when no
database is reachable. All work happens in one transaction that is always rolled back.
"""
from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from app.services.dag_ingest import _resolve_story_repo_aliases
from app.services.git_graph.repo_identity import canonical_repo_id
from app.services import local_workspace_nodes as lwn
from app.services.local_workspace_nodes import (
    LOCAL_CLONE_SUBDIR,
    record_local_workspace_node,
)

# Reuse the connection/skip/rollback machinery so the DSN-resolution and
# always-rollback guarantees live in exactly one place.
from app.services.tests.test_story_repo_scope_sql import (  # noqa: PLC0415
    PREFIX,
    _connect_or_skip,
)


class _PoolShim:
    """Presents one connection as a pool.

    `_record_story_workspace_nodes` (reused from the gateway provisioner) does
    `async with pool.acquire() as conn, conn.transaction():`, so it needs the pool
    interface. These tests hold a single connection inside an outer transaction they
    roll back, so the shim hands that same connection over and never closes it —
    asyncpg turns the inner `transaction()` into a SAVEPOINT, which keeps the outer
    rollback authoritative. Test-only: production passes a real pool.
    """

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        shim = self

        @contextlib.asynccontextmanager
        async def _acquire():
            yield shim._conn

        return _acquire()

    def __getattr__(self, name):
        return getattr(self._conn, name)


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
            "SELECT count(*) FROM story_workspace_nodes WHERE story_id LIKE $1",
            f"{PREFIX}%")
        await conn.close()
        assert leaked == 0, f"{leaked} node rows survived rollback — dev DB dirtied"


async def _story(conn, sid: str, execution_mode: str = "local"):
    await conn.execute("INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                       f"{sid}-goal", f"fixture {sid}", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, created_by, execution_mode) "
        "VALUES ($1,$2,$3,$4,$5)",
        sid, f"{sid}-goal", "node fixture", f"{PREFIX}-user", execution_mode)


async def _node(conn, sid: str, owner: str, name: str, branch: str, subdir: str):
    await conn.execute(
        """INSERT INTO story_workspace_nodes
               (story_id, git_repo_id, branch_name, subdir, is_write,
                repo_owner, repo_name)
           VALUES ($1,$2,$3,$4,TRUE,$5,$6)""",
        sid, canonical_repo_id(owner, name), branch, subdir, owner, name)


@pytest.mark.asyncio
async def test_local_checkout_records_one_node_with_the_extension_subdir():
    """`subdir` must be 'repo' — the directory the extension actually clones into,
    NOT the gateway's `repos/<owner>__<name>` convention."""
    async with _conn() as conn:
        sid = f"{PREFIX}-wsn-a"
        await _story(conn, sid)

        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/story-1",
                "clone_url": "https://github.com/acme/svc-a.git", "trunk": "main"}
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)):
            assert await record_local_workspace_node(_PoolShim(conn), sid) is True

        rows = await conn.fetch(
            "SELECT git_repo_id, branch_name, subdir, is_write, repo_owner, repo_name "
            "FROM story_workspace_nodes WHERE story_id = $1", sid)
        assert len(rows) == 1, "local execution is single-repo by construction"
        row = rows[0]
        assert row["subdir"] == LOCAL_CLONE_SUBDIR == "repo"
        assert row["git_repo_id"] == canonical_repo_id("acme", "svc-a")
        assert row["branch_name"] == "feat/story-1"
        assert row["is_write"] is True
        assert (row["repo_owner"], row["repo_name"]) == ("acme", "svc-a")


@pytest.mark.asyncio
async def test_no_node_recorded_before_the_branch_is_cut():
    """`_repo_ref` returns None until the engine cuts the branch (and refuses to
    guess when several repos are live). Recording nothing is correct — a row would
    describe a clone that does not exist."""
    async with _conn() as conn:
        sid = f"{PREFIX}-wsn-b"
        await _story(conn, sid)
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=None)):
            assert await record_local_workspace_node(_PoolShim(conn), sid) is False
        assert await conn.fetchval(
            "SELECT count(*) FROM story_workspace_nodes WHERE story_id = $1", sid) == 0


@pytest.mark.asyncio
async def test_recording_is_idempotent_across_repeated_checkouts():
    """Checkout/resume can run repeatedly; the node set must not accumulate rows."""
    async with _conn() as conn:
        sid = f"{PREFIX}-wsn-c"
        await _story(conn, sid)
        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/x",
                "clone_url": "u", "trunk": "main"}
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)):
            await record_local_workspace_node(_PoolShim(conn), sid)
            await record_local_workspace_node(_PoolShim(conn), sid)
        assert await conn.fetchval(
            "SELECT count(*) FROM story_workspace_nodes WHERE story_id = $1", sid) == 1


@pytest.mark.asyncio
async def test_git_credential_precondition_is_satisfied():
    """The exact query behind the 404 must now return a row for a local story."""
    async with _conn() as conn:
        sid = f"{PREFIX}-wsn-d"
        await _story(conn, sid)
        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/x",
                "clone_url": "u", "trunk": "main"}
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)):
            await record_local_workspace_node(_PoolShim(conn), sid)

        rows = await conn.fetch(
            "SELECT repo_name, repo_owner, is_write FROM story_workspace_nodes "
            "WHERE story_id = $1", sid)
        assert rows, "git_credential would still raise 404 for this local story"


@pytest.mark.asyncio
async def test_local_story_gets_no_repo_aliases_even_with_nodes():
    """Prompt-preservation guard. With a node present, alias resolution must STILL
    return empty for a local story, so `tasks.repo_subdir` stays NULL and the local
    dispatch prompt is byte-identical to before nodes were recorded."""
    async with _conn() as conn:
        sid = f"{PREFIX}-wsn-e"
        await _story(conn, sid, execution_mode="local")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repo")

        aliases, subdir_by_alias = await _resolve_story_repo_aliases(conn, sid)
        assert aliases == set() and subdir_by_alias == {}, (
            "a local story must not resolve repo aliases — it would emit "
            "'cd repo/' into a session whose cwd is already that directory"
        )


@pytest.mark.asyncio
async def test_operator_story_still_resolves_aliases():
    """The skip must be targeted, not blanket: an operator (gateway) story with the
    same node must still resolve, or multi-repo gateway scoping would silently break."""
    async with _conn() as conn:
        sid = f"{PREFIX}-wsn-f"
        await _story(conn, sid, execution_mode="operator")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")

        aliases, subdir_by_alias = await _resolve_story_repo_aliases(conn, sid)
        assert subdir_by_alias, "operator stories must keep resolving repo aliases"
        assert subdir_by_alias.get("repos/acme__svc-a") == "repos/acme__svc-a"
        assert subdir_by_alias.get("acme/svc-a") == "repos/acme__svc-a"
        assert subdir_by_alias.get("svc-a") == "repos/acme__svc-a", (
            "bare name is offered when unambiguous"
        )


@pytest.mark.asyncio
async def test_multi_repo_local_story_DOES_resolve_aliases():
    """The skip is keyed on repo COUNT, not `execution_mode` alone (C8,
    research/NREPO_LOCAL_EXECUTION_PLAN.md): a multi-repo local checkout has extra
    repos the agent's cwd is NOT already inside, so — unlike the single-repo case
    above — a task genuinely needs to know which repo it targets."""
    async with _conn() as conn:
        sid = f"{PREFIX}-wsn-g"
        await _story(conn, sid, execution_mode="local")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        await _node(conn, sid, "acme", "svc-b", "feat/x", "repos/acme__svc-b")

        aliases, subdir_by_alias = await _resolve_story_repo_aliases(conn, sid)
        assert subdir_by_alias, "a multi-repo local story must resolve repo aliases"
        assert subdir_by_alias.get("repos/acme__svc-a") == "repos/acme__svc-a"
        assert subdir_by_alias.get("repos/acme__svc-b") == "repos/acme__svc-b"


# ──────────────────────────────────────────────────────────────────────────
# Provenance discriminator: which layout a NEW row gets.
#
# The safety property: a story that was checked out before has a clone sitting in
# `repo/` on someone's disk. Recording anything else for it would point every consumer
# at a directory that does not exist and orphan that work. So "unknown" must resolve to
# legacy, never to the new convention.
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("first_checkout", [False, None])
async def test_previously_checked_out_or_unknown_stays_legacy(first_checkout):
    """`False` (checked out before) and `None` (provenance unknown) must BOTH record the
    legacy dir, even with the repos layout switched on for new workspaces."""
    async with _conn() as conn:
        sid = f"{PREFIX}-prov-{'f' if first_checkout is False else 'n'}"
        await _story(conn, sid)
        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/x",
                "clone_url": "u", "trunk": "main"}
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)), \
             patch.object(lwn, "USE_REPOS_LAYOUT_FOR_NEW_WORKSPACES", True):
            await record_local_workspace_node(
                _PoolShim(conn), sid, first_checkout=first_checkout,
                path_contract=True,
            )
        assert await conn.fetchval(
            "SELECT subdir FROM story_workspace_nodes WHERE story_id = $1", sid,
        ) == "repo", "a possibly-existing clone must never be relocated by a convention change"


@pytest.mark.asyncio
async def test_first_checkout_adopts_the_new_layout_when_enabled():
    """Only a genuine first checkout — nothing on disk yet — may use `repos/…`. This is
    also the case for an OLD story that was never imported: what matters is that no clone
    exists, not the story's age."""
    async with _conn() as conn:
        sid = f"{PREFIX}-prov-first"
        await _story(conn, sid)
        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/x",
                "clone_url": "u", "trunk": "main"}
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)), \
             patch.object(lwn, "USE_REPOS_LAYOUT_FOR_NEW_WORKSPACES", True):
            await record_local_workspace_node(
                _PoolShim(conn), sid, first_checkout=True, path_contract=True)
        assert await conn.fetchval(
            "SELECT subdir FROM story_workspace_nodes WHERE story_id = $1", sid,
        ) == "repos/acme__svc-a"


@pytest.mark.asyncio
async def test_a_client_that_does_not_declare_the_contract_stays_legacy():
    """THE guard that lets the layout flip ship safely.

    An installed extension cannot be upgraded on demand, and one that still joins a
    hardcoded "repo" will create exactly that directory. Recording `repos/…` for it would
    describe a directory that never comes into existence — so all six readers, the plan
    path, the resources doc and the DAG scoping would point somewhere empty.

    So `path_contract=False` must keep the legacy layout even with the kill switch ON and
    a genuine first checkout. Absent the declaration, legacy is not a fallback: it is the
    correct description of what that client does."""
    async with _conn() as conn:
        sid = f"{PREFIX}-prov-nocap"
        await _story(conn, sid)
        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/x",
                "clone_url": "u", "trunk": "main"}
        assert lwn.USE_REPOS_LAYOUT_FOR_NEW_WORKSPACES is True, (
            "this test is only meaningful while the switch is on"
        )
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)):
            await record_local_workspace_node(
                _PoolShim(conn), sid, first_checkout=True, path_contract=False)
        assert await conn.fetchval(
            "SELECT subdir FROM story_workspace_nodes WHERE story_id = $1", sid,
        ) == "repo"


@pytest.mark.asyncio
async def test_the_kill_switch_still_overrides_a_declaring_client():
    """The switch remains a real kill switch: turning it off reverts every new workspace
    to the legacy layout regardless of what the client declares, so the rollout can be
    stopped without shipping an extension release."""
    async with _conn() as conn:
        sid = f"{PREFIX}-prov-killswitch"
        await _story(conn, sid)
        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/x",
                "clone_url": "u", "trunk": "main"}
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)), \
             patch.object(lwn, "USE_REPOS_LAYOUT_FOR_NEW_WORKSPACES", False):
            await record_local_workspace_node(
                _PoolShim(conn), sid, first_checkout=True, path_contract=True)
        assert await conn.fetchval(
            "SELECT subdir FROM story_workspace_nodes WHERE story_id = $1", sid,
        ) == "repo"


@pytest.mark.asyncio
async def test_existing_row_survives_a_convention_change():
    """The freeze outranks the discriminator: a recorded clone is never moved, even for
    a story we would otherwise treat as a first checkout."""
    async with _conn() as conn:
        sid = f"{PREFIX}-prov-freeze"
        await _story(conn, sid)
        await _node(conn, sid, "acme", "svc-a", "feat/old", "repo")
        repo = {"owner": "acme", "repo": "svc-a", "branch": "feat/new",
                "clone_url": "u", "trunk": "main"}
        with patch("app.services.local_hydrate._repo_ref", AsyncMock(return_value=repo)), \
             patch.object(lwn, "USE_REPOS_LAYOUT_FOR_NEW_WORKSPACES", True):
            await record_local_workspace_node(
                _PoolShim(conn), sid, first_checkout=True, path_contract=True)
        rows = await conn.fetch(
            "SELECT subdir, branch_name FROM story_workspace_nodes WHERE story_id = $1", sid)
        assert [r["subdir"] for r in rows] == ["repo"], "subdir frozen"
        assert rows[0]["branch_name"] == "feat/new", "branch still updates on a re-cut"


# ──────────────────────────────────────────────────────────────────────────
# Resolver ORDERING, against a real database.
#
# Load-bearing, not cosmetic: `primary_write_subdir` reports the FIRST write clone as
# the story's plan directory, so a change in this order silently relocates plans. It
# must match `_clone_descriptors` and the old per-site queries exactly:
# write repos first, then subdir ascending.
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolver_orders_write_repos_first_then_insertion_order():
    """2026-08-20 fix: ordering used to be `subdir ASC` (alphabetical) — which let a
    reimport, re-deriving the same rows in a different query, hand primacy to a
    DIFFERENT repo than the one actually cloned first (the real incident: two
    `.agentos/bridge.json` files, disagreeing about which repo was primary). Now
    it's insertion order (`created_at ASC`, falling back to physical/heap order on
    a same-transaction tie — see `resolve_workspace_clones`'s docstring), so the
    repo recorded FIRST stays entry 0 regardless of subdir naming."""
    from app.services.story_workspace_layout import resolve_workspace_clones, write_clones
    async with _conn() as conn:
        sid = f"{PREFIX}-order"
        await _story(conn, sid)
        # Insert deliberately with a read-only clone that sorts FIRST alphabetically,
        # and the two write repos in an order that does NOT match alphabetical subdir
        # — the write repos' relative order must survive as inserted (zzz before mmm).
        await conn.execute(
            """INSERT INTO story_workspace_nodes
                   (story_id, git_repo_id, branch_name, subdir, is_write,
                    repo_owner, repo_name)
               VALUES ($1,'gr_ctx','feat/x','repos/aaa__context',FALSE,'aaa','context'),
                      ($1,'gr_z','feat/x','repos/zzz__late',TRUE,'zzz','late'),
                      ($1,'gr_m','feat/x','repos/mmm__early',TRUE,'mmm','early')""",
            sid,
        )
        clones = await resolve_workspace_clones(conn, sid)
        assert [c["subdir"] for c in clones] == [
            "repos/zzz__late", "repos/mmm__early", "repos/aaa__context",
        ], "write repos must precede read-only, each group in INSERTION order"

        writes = write_clones(clones)
        assert writes[0]["subdir"] == "repos/zzz__late", (
            "the FIRST-INSERTED write clone is the story's plan directory — a "
            "read-only clone, or a later-inserted write clone, must never outrank it"
        )


@pytest.mark.asyncio
async def test_primary_write_subdir_agrees_with_the_resolver():
    """The plan directory and the resolver must never disagree — that divergence is
    what `_clone_descriptors` warns sends the assistant to a directory that isn't there."""
    from app.services.operator_provisioning import primary_write_subdir
    from app.services.story_workspace_layout import resolve_workspace_clones, write_clones
    async with _conn() as conn:
        sid = f"{PREFIX}-primary"
        await _story(conn, sid)
        await conn.execute(
            """INSERT INTO story_workspace_nodes
                   (story_id, git_repo_id, branch_name, subdir, is_write,
                    repo_owner, repo_name)
               VALUES ($1,'gr_b','feat/x','repos/bbb__two',TRUE,'bbb','two'),
                      ($1,'gr_a','feat/x','repos/aaa__one',TRUE,'aaa','one')""",
            sid,
        )
        expected = write_clones(await resolve_workspace_clones(conn, sid))[0]["subdir"]
        assert await primary_write_subdir(conn, sid) == expected == "repos/bbb__two", (
            "the FIRST-INSERTED row (bbb__two) is primary, not whichever subdir sorts "
            "first alphabetically (aaa__one) — see 2026-08-20 fix"
        )


@pytest.mark.asyncio
async def test_primary_write_subdir_falls_back_to_legacy_for_an_unrecorded_story():
    from app.services.operator_provisioning import primary_write_subdir
    async with _conn() as conn:
        sid = f"{PREFIX}-primary-legacy"
        await _story(conn, sid)
        assert await primary_write_subdir(conn, sid) == "repo"


# ──────────────────────────────────────────────────────────────────────────
# The plan root must survive a repository grant, against a real database.
#
# Before the 2026-08-20 fix, `primary_write_subdir` re-derived off alphabetical
# `subdir` ordering, so granting a SECOND repo whose directory happened to sort
# earlier would flip which repo it considered primary — even though the plan was
# already written to disk in the first repo. `resolve_plan_root`'s job is to
# survive that regardless (it reads the recorded `current_plan_path`, not a
# re-derived subdir) — that protection is unconditional and still tested here.
# `primary_write_subdir` itself no longer flips either now that ordering follows
# INSERTION order rather than alphabetical subdir — both are pinned below so a
# regression in either shows up.
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_root_survives_a_later_repo_grant_that_sorts_earlier():
    from app.services.operator_provisioning import primary_write_subdir
    from app.services.story_plan_root import resolve_plan_root
    async with _conn() as conn:
        sid = f"{PREFIX}-planroot"
        await _story(conn, sid)

        # The story planned into zzz__late, and that path is recorded.
        await _node(conn, sid, "zzz", "late", "feat/x", "repos/zzz__late")
        await conn.execute(
            "UPDATE stories SET current_plan_path = $2 WHERE id = $1",
            sid, f"repos/zzz__late/stories/{sid}/plans/cycle-01-x.md")
        assert await resolve_plan_root(conn, sid) == "repos/zzz__late"

        # Now a second repo is granted whose directory sorts FIRST alphabetically —
        # but was inserted SECOND.
        await _node(conn, sid, "aaa", "early", "feat/x", "repos/aaa__early")

        assert await primary_write_subdir(conn, sid) == "repos/zzz__late", (
            "insertion order wins — the later grant (aaa__early) must NOT become "
            "primary just because its name sorts first"
        )
        assert await resolve_plan_root(conn, sid) == "repos/zzz__late", (
            "the recorded plan root must NOT move; the plan is on disk in zzz__late"
        )


@pytest.mark.asyncio
async def test_plan_root_falls_back_to_a_prior_version_then_to_the_layout():
    from app.services.story_plan_root import resolve_plan_root
    async with _conn() as conn:
        sid = f"{PREFIX}-planroot-ver"
        await _story(conn, sid)
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")

        # No plan at all yet -> the layout is the only available answer.
        assert await resolve_plan_root(conn, sid) == "repos/acme__svc-a"

        # A prior version records the root even with the story pointer empty
        # (replan). It names a directory this story really has, so it stands
        # even though the layout on its own would have chosen svc-a.
        await _node(conn, sid, "acme", "svc-b", "feat/x", "repos/acme__svc-b")
        await conn.execute(
            """INSERT INTO plan_versions (id, story_id, version, plan_doc_path, status)
               VALUES (gen_random_uuid(), $1, 1, $2, 'approved')""",
            sid, f"repos/acme__svc-b/stories/{sid}/plans/cycle-01-x.md")
        assert await resolve_plan_root(conn, sid) == "repos/acme__svc-b", (
            "a cleared current_plan_path must not lose the root a prior version recorded"
        )


@pytest.mark.asyncio
async def test_a_recorded_root_that_is_no_longer_a_clone_gives_way_to_the_layout():
    """The re-provisioning case, and the reason a recorded root is not absolute.

    Provisioning puts every repo under `repos/<owner>__<name>`. A story
    carried over from the single-`repo/` layout still records `repo/` in its
    plan history while its clone now lives somewhere else. Honouring the
    stale root would put the validate-plan gate and the CLAUDE.md the agent
    is reading in different directories -- the agent writes exactly where it
    was told and the gate rejects it for doing so.

    This case used to be asserted the other way round, expecting `repo`,
    which is the answer `_root_still_exists` was written to stop returning.
    """
    from app.services.story_plan_root import resolve_plan_root
    async with _conn() as conn:
        sid = f"{PREFIX}-planroot-stale"
        await _story(conn, sid)
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        await conn.execute(
            """INSERT INTO plan_versions (id, story_id, version, plan_doc_path, status)
               VALUES (gen_random_uuid(), $1, 1, $2, 'approved')""",
            sid, f"repo/stories/{sid}/plans/cycle-01-x.md")
        assert await resolve_plan_root(conn, sid) == "repos/acme__svc-a", (
            "a plan root naming a directory this story no longer has must give "
            "way to the layout"
        )
