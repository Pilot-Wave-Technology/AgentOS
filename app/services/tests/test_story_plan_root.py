"""Where a story's plans live must not move once they exist.

Two places must agree exactly on the plan root: the gate that tells the agent where to
write (`validate_plan`) and the commit that strips the root to get a repo-relative path
(`_push_plan_to_repo`). They used to DERIVE it as the alphabetically-first write repo,
which means granting a repository sorting earlier silently changed the expected location
mid-story — and the gate would then reject the exact path the agent had been told to use.

The path is already recorded, including the root, on both `stories.current_plan_path` and
`plan_versions.plan_doc_path`. Reading it removes the whole failure mode: a story that has
a plan keeps its plan root for life.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.story_plan_root import plan_root_of, resolve_plan_root


# ── parsing ───────────────────────────────────────────────────────────────

def test_a_multi_repo_root_containing_a_slash_is_kept_whole():
    """The case a first-path-segment split gets wrong: `repos/acme__web` IS the root, so
    taking the first segment would return `repos` and every plan path would be wrong."""
    assert plan_root_of("repos/acme__web/stories/s1/plans/cycle-01-x.md") == "repos/acme__web"


def test_a_legacy_root_is_kept():
    assert plan_root_of("repo/stories/s1/plans/cycle-01-x.md") == "repo"


def test_a_plan_at_the_workspace_root_has_no_root():
    """A shape the gate already rejects. Returning "" makes callers fall through to the
    layout rather than inventing a root from a malformed path."""
    for p in ["plans/cycle-01-x.md", "", None, "stories/s1/plans/x.md"]:
        assert plan_root_of(p) == "", f"p={p!r}"


# ── resolution order ──────────────────────────────────────────────────────

def _pool(current=None, version_path=None, raise_on_read=False):
    pool = AsyncMock()
    if raise_on_read:
        pool.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        pool.fetchval = AsyncMock(side_effect=[current, version_path])
    return pool


@pytest.mark.asyncio
async def test_the_recorded_story_path_wins():
    pool = _pool(current="repos/acme__web/stories/s1/plans/cycle-02-x.md")
    with patch("app.services.operator_provisioning.primary_write_subdir",
               AsyncMock(return_value="repos/aaa__first")) as derive:
        assert await resolve_plan_root(pool, "s1") == "repos/acme__web"
    derive.assert_not_awaited(), "a recorded root must never be re-derived"


@pytest.mark.asyncio
async def test_a_prior_plan_version_is_used_when_the_pointer_is_empty():
    """Covers a story mid-replan: `current_plan_path` can be cleared while previous
    versions still record where plans live."""
    pool = _pool(current=None, version_path="repo/stories/s1/plans/cycle-01-x.md")
    with patch("app.services.operator_provisioning.primary_write_subdir",
               AsyncMock(return_value="repos/aaa__first")) as derive:
        assert await resolve_plan_root(pool, "s1") == "repo"
    derive.assert_not_awaited()


@pytest.mark.asyncio
async def test_derivation_is_the_first_plan_fallback_only():
    """A story with no plan yet has nothing recorded, so the layout is the only answer."""
    pool = _pool(current=None, version_path=None)
    with patch("app.services.operator_provisioning.primary_write_subdir",
               AsyncMock(return_value="repos/acme__web")) as derive:
        assert await resolve_plan_root(pool, "s1") == "repos/acme__web"
    derive.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_read_failure_degrades_to_derivation():
    """Neither caller may be broken by a transient read — the derived answer is what they
    used before this module existed, so the worst case is the previous behaviour."""
    pool = _pool(raise_on_read=True)
    with patch("app.services.operator_provisioning.primary_write_subdir",
               AsyncMock(return_value="repo")):
        assert await resolve_plan_root(pool, "s1") == "repo"


# ── the two consumers must resolve identically ────────────────────────────

def test_the_gate_and_the_commit_read_the_root_the_same_way():
    """Pinned by source, because a divergence here is silent: the gate would reject the
    path the agent was told to write, or an approved plan would never be committed."""
    import inspect
    from app.routers import internal_assistant
    from app.services import plan_approval

    gate = inspect.getsource(internal_assistant.validate_plan)
    commit = inspect.getsource(plan_approval._push_plan_to_repo)
    for name, src in [("validate_plan", gate), ("_push_plan_to_repo", commit)]:
        assert "resolve_plan_root" in src, f"{name} must use the shared resolver"
        assert "primary_write_subdir" not in src, (
            f"{name} must not re-derive the root — that is the relocation bug"
        )
