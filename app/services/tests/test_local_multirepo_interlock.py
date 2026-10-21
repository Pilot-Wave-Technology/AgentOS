"""The multi-repo × local-execution interlock must refuse, not guess.

Covers the three enforcement points added for decision D3 in
`research/LOCAL_EXEC_MULTIREPO_INTEGRATION.md`:

  1. `local_checkout._import_readiness` — refuses a local checkout of a multi-repo story
  2. `resource_allocation` — refuses a 2nd repo grant on a locally-claimed story (see note)
  3. `local_hydrate._repo_ref` — refuses to resolve one repo when several are live

**Scope of these tests — read before trusting them.** The pool is mocked, so the
SQL in `story_repo_scope` is NOT exercised here. What is verified is the Python-side
decision logic: that a refusal happens, that it happens in the right ORDER relative
to the resume short-circuit, and that `_repo_ref` distinguishes several nodes in one
repo from several repos.

The SQL itself — DISTINCT-repo counting and the `checkout_expires_at > NOW()`
comparison — is covered by `test_story_repo_scope_sql.py`, which runs against a real
PostgreSQL. **Start the local stack (`local-development/scripts/stack.ps1`) and test
there first**; those tests pick up the local database automatically and skip only
when none is reachable. Deployment is not required to verify this.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import local_checkout as lc
from app.services import local_hydrate as lh
from app.services import story_repo_scope as srs


def _story_row(**overrides):
    row = {
        "state": "accepted",
        "execution_mode": "",
        "depends_on": [],
        "story_kind": "code",
        "created_by": "user-1",
    }
    row.update(overrides)
    return row


def _node(owner: str, name: str, branch: str = "feat/x", created_at: int = 0):
    return {
        "owner": owner, "name": name, "clone_url": f"https://x/{owner}/{name}.git",
        "branch_name": branch, "trunk_branch": "main",
        # Real rows carry the node's created_at (used to order MULTIPLE distinct
        # repos deterministically — see local_hydrate._resolve_live_repos's doc
        # comment). Defaulted equal here since these tests only exercise the
        # single-repo shim `_repo_ref`, which never reaches that ordering.
        "created_at": created_at,
    }


# --------------------------------------------------------------------------
# 1. local checkout refusal
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_refused_for_multi_repo_story():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_story_row())

    with patch.object(lc, "count_story_repos", AsyncMock(return_value=2)):
        refusal = await lc._import_readiness(pool, "story-1", "goal-1")

    assert refusal is not None, "a 2-repo story must not be importable locally"
    reason, message = refusal
    assert reason == "multi_repo_unsupported"
    assert "2 repositories" in message
    # The message must name the consequence, not just declare it unsupported.
    assert "landed" in message


@pytest.mark.asyncio
async def test_single_repo_story_still_importable():
    """The guard must not regress the normal one-repo path."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_story_row())

    with patch.object(lc, "count_story_repos", AsyncMock(return_value=1)):
        assert await lc._import_readiness(pool, "story-1", "goal-1") is None

    # A story with no repo grant yet is also fine — the branch simply is not cut.
    with patch.object(lc, "count_story_repos", AsyncMock(return_value=0)):
        assert await lc._import_readiness(pool, "story-1", "goal-1") is None


@pytest.mark.asyncio
async def test_multi_repo_refusal_precedes_the_resume_shortcircuit():
    """ORDERING regression guard.

    An already-local, in_progress story returns None early as a "resume". If the
    multi-repo check sat after that branch, a story that acquired a second repo
    mid-flight would resume and silently work whichever repo `_repo_ref` picked —
    the exact silent mis-landing the interlock exists to prevent.
    """
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_story_row(
        execution_mode="local", state="in_progress",
    ))

    with patch.object(lc, "count_story_repos", AsyncMock(return_value=3)):
        refusal = await lc._import_readiness(pool, "story-1", "goal-1")

    assert refusal is not None, (
        "multi-repo must be refused even on resume — otherwise the check is dead "
        "code for every story already checked out"
    )
    assert refusal[0] == "multi_repo_unsupported"


# --------------------------------------------------------------------------
# 2. _repo_ref must refuse ambiguity, but tolerate re-cuts in one repo
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repo_ref_returns_none_when_several_repos_are_live():
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[_node("acme", "svc-a"), _node("acme", "svc-b")])
    pool.fetchval = AsyncMock(return_value=None)  # no sticky primary recorded yet

    assert await lh._repo_ref(pool, "story-1") is None, (
        "two live repos is ambiguous — resolving one silently is what let a push "
        "token be minted for a repo the user never cloned"
    )


@pytest.mark.asyncio
async def test_repo_ref_tolerates_several_nodes_in_one_repo():
    """Re-cut / reconcile leaves multiple live nodes for the SAME repo. That is the
    normal single-repo case and must still resolve, or the guard would break every
    ordinary local checkout."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[
        _node("acme", "svc-a", "feat/new"),
        _node("acme", "svc-a", "feat/old"),
    ])

    ref = await lh._repo_ref(pool, "story-1")
    assert ref is not None
    assert ref["repo"] == "svc-a"
    assert ref["branch"] == "feat/new", "must keep the updated_at DESC ordering"


@pytest.mark.asyncio
async def test_repo_ref_none_when_no_branch_cut_yet():
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    assert await lh._repo_ref(pool, "story-1") is None


# --------------------------------------------------------------------------
# 3. checkout-liveness predicate + shared message
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_live_checkout_when_query_matches_nothing():
    """The expiry comparison is in SQL; this only pins the None -> False mapping,
    which is what stops a stale lock blocking a grant forever."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await srs.has_live_local_checkout(pool, "story-1") is False


@pytest.mark.asyncio
async def test_live_checkout_detected():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={
        "checked_out_to_uid": "uid-1", "checkout_expires_at": "2099-01-01T00:00:00Z",
    })
    assert await srs.has_live_local_checkout(pool, "story-1") is True


def test_refusal_message_names_the_repo_count_and_the_way_forward():
    msg = srs.multi_repo_local_refusal_message(4)
    assert "4 repositories" in msg
    assert "split" in msg, "must offer the documented alternative, not just refuse"


# --------------------------------------------------------------------------
# 4. hydrate never serves a repo the extension would clone UNRECORDED
#
# The absence of a `story_workspace_nodes` row is the system-wide "legacy workspace"
# flag. A clone that happened without one would read as legacy forever, sending every
# consumer to `repo/` — so hydrate, the only thing that hands the extension a repo,
# must refuse rather than serve one it could not record.
# --------------------------------------------------------------------------

_STORY_ROW = {
    "id": "story-1", "title": "t", "goal_id": "goal-1",
    "current_phase": "", "story_kind": "code", "execution_mode": "local",
    "current_plan_id": None, "current_plan_path": None,
}
_REPO = {"owner": "acme", "repo": "svc-a", "clone_url": "u",
         "branch": "feat/x", "trunk": "main"}


def _hydrate_patches(*, repo, record):
    """Patch out everything but the record-then-serve decision under test."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_STORY_ROW)
    return [
        patch.object(lh, "get_pool", lambda: pool),
        patch.object(lh, "_repo_ref", AsyncMock(return_value=repo)),
        patch.object(lh, "_persona", AsyncMock(return_value={"files": {}, "skills": {}})),
        patch("app.services.local_bundle.build_local_bundle",
              AsyncMock(return_value={"agent_md": "", "claude_md": "", "skills": {}})),
        patch("app.services.local_workspace_nodes.record_local_workspace_node", record),
    ]


async def _hydrate(*, repo, record):
    with __import__("contextlib").ExitStack() as stack:
        for p in _hydrate_patches(repo=repo, record=record):
            stack.enter_context(p)
        return await lh.build_hydration_bundle("story-1")


@pytest.mark.asyncio
async def test_hydrate_refuses_when_the_layout_cannot_be_recorded():
    result = await _hydrate(repo=_REPO, record=AsyncMock(return_value=False))
    assert result["ok"] is False
    assert result["reason"] == "workspace_record_failed"
    assert "repo" not in result, "must not hand over a repo it could not record"


@pytest.mark.asyncio
async def test_hydrate_refuses_when_recording_raises():
    """A raised error must not be swallowed into a served bundle."""
    result = await _hydrate(repo=_REPO, record=AsyncMock(side_effect=RuntimeError("db down")))
    assert result["ok"] is False
    assert result["reason"] == "workspace_record_failed"


@pytest.mark.asyncio
async def test_hydrate_serves_the_repo_once_recorded():
    record = AsyncMock(return_value=True)
    result = await _hydrate(repo=_REPO, record=record)
    assert result["ok"] is True
    assert result["repo"] == _REPO
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_branch_yet_still_hydrates_without_recording():
    """Regression guard for the pre-existing flow. Before the branch is cut there is
    nothing to record and nothing to clone — the extension shows "the engine hasn't cut
    this story's feature branch yet" and the user retries. That must keep working, so
    recording is attempted ONLY when a repo actually resolved."""
    record = AsyncMock(return_value=False)
    result = await _hydrate(repo=None, record=record)
    assert result["ok"] is True, "must not turn a not-yet-cut branch into a failure"
    assert result["repo"] is None
    record.assert_not_awaited()


# --------------------------------------------------------------------------
# 5. The wire contract: `repos` carries the layout, and its ABSENCE is the flag
# --------------------------------------------------------------------------

_CLONE = {
    "git_repo_id": "gr_abc", "branch_name": "feat/x", "subdir": "repo",
    "is_write": True, "repo_owner": "acme", "repo_name": "svc-a",
}


async def _hydrate_with_clones(clones):
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_STORY_ROW)
    with __import__("contextlib").ExitStack() as stack:
        for p in [
            patch.object(lh, "get_pool", lambda: pool),
            patch.object(lh, "_repo_ref", AsyncMock(return_value=_REPO)),
            patch.object(lh, "_persona", AsyncMock(return_value={"files": {}, "skills": {}})),
            patch.object(lh, "resolve_workspace_clones", AsyncMock(return_value=clones)),
            patch("app.services.local_bundle.build_local_bundle",
                  AsyncMock(return_value={"agent_md": "", "claude_md": "", "skills": {}})),
            patch("app.services.local_workspace_nodes.record_local_workspace_node",
                  AsyncMock(return_value=True)),
        ]:
            stack.enter_context(p)
        return await lh.build_hydration_bundle("story-1")


@pytest.mark.asyncio
async def test_legacy_workspace_omits_repos_entirely():
    """Absence is the flag. An EMPTY array would be indistinguishable from "recorded,
    but zero repos" — a state that does not exist — so the key must not appear at all."""
    bundle = await _hydrate_with_clones([])
    assert "repos" not in bundle, "a legacy workspace must not publish a repos key"
    assert bundle["repo"] == _REPO, "the singular field still serves older clients"


@pytest.mark.asyncio
async def test_recorded_workspace_publishes_the_path():
    bundle = await _hydrate_with_clones([_CLONE])
    assert bundle["repos"] == [{
        "name": "acme/svc-a", "path": "repo", "branch": "feat/x", "write": True,
    }], "shape must match the gateway snapshot so clients need one code path"


@pytest.mark.asyncio
async def test_repos_is_published_even_for_a_single_repo():
    """Deliberately unlike the gateway snapshot, which only includes `repos` when there
    is more than one. Here `path` is the addressing scheme, so one repo needs it too —
    that is the whole point of moving layout ownership off the client."""
    bundle = await _hydrate_with_clones([{**_CLONE, "subdir": "repos/acme__svc-a"}])
    assert len(bundle["repos"]) == 1
    assert bundle["repos"][0]["path"] == "repos/acme__svc-a"
