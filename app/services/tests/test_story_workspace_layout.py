"""The shared workspace-layout resolver, and the one thing consolidation nearly broke.

Six call sites used to run their own `story_workspace_nodes` query. Consolidating them
looked like pure tidying, but they did not merely differ in how they handled ABSENCE —
they differed in how they handled FAILURE, for a good reason:

  * `diff_review._story_write_repo_count` RAISES on an unexpected error, because quietly
    answering "one repo" would diff and commit only the primary repo while the human
    approves what they believe is the whole change — a silently partial multi-repo
    landing.
  * everyone else degrades to the legacy answer, which is harmless for them: a resources
    document, a state snapshot, a plan directory, DAG alias validation.

A resolver that always swallowed errors would have regressed `diff_review` into exactly
the bug its own comment warns about. Hence `strict`, and hence these tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services.story_workspace_layout import (
    is_legacy_workspace,
    resolve_workspace_clones,
    to_bundle_repos,
    write_clones,
)


def _clone(owner="acme", name="svc-a", subdir="repo", is_write=True, branch="feat/x"):
    return {
        "git_repo_id": f"gr_{name}", "branch_name": branch, "subdir": subdir,
        "is_write": is_write, "repo_owner": owner, "repo_name": name,
    }


def _pool(*, rows=None, error=None):
    pool = AsyncMock()
    pool.fetch = AsyncMock(side_effect=error) if error else AsyncMock(return_value=rows or [])
    return pool


# ── absence is an answer ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_rows_is_a_legacy_workspace_not_an_error():
    pool = _pool(rows=[])
    assert await resolve_workspace_clones(pool, "s1") == []
    assert await is_legacy_workspace(pool, "s1") is True


@pytest.mark.asyncio
async def test_rows_present_means_not_legacy():
    pool = _pool(rows=[_clone()])
    assert await is_legacy_workspace(pool, "s1") is False


# ── failure handling: the distinction that matters ────────────────────────

@pytest.mark.asyncio
async def test_missing_table_degrades_even_in_strict_mode():
    """The subtle case. An un-migrated database legitimately has no layouts, so the
    legacy answer is CORRECT — including for the strict caller. `strict` must only
    change what a genuine fault means, never what a missing table means."""
    pool = _pool(error=asyncpg.UndefinedTableError("no such table"))
    assert await resolve_workspace_clones(pool, "s1") == []
    assert await resolve_workspace_clones(pool, "s1", strict=True) == []


@pytest.mark.asyncio
async def test_unexpected_failure_degrades_by_default():
    pool = _pool(error=RuntimeError("connection reset"))
    assert await resolve_workspace_clones(pool, "s1") == []


@pytest.mark.asyncio
async def test_unexpected_failure_raises_when_strict():
    """`diff_review` depends on this. A visible error the human can retry beats a
    plausible wrong answer that lands half a story."""
    pool = _pool(error=RuntimeError("column repo_owner does not exist"))
    with pytest.raises(RuntimeError):
        await resolve_workspace_clones(pool, "s1", strict=True)


@pytest.mark.asyncio
async def test_diff_review_count_propagates_the_strict_failure():
    """Pins the wiring, not just the resolver: the count must still raise, which is the
    behaviour `_story_write_repo_count` had before it was consolidated."""
    from app.services.diff_review import _story_write_repo_count
    pool = _pool(error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await _story_write_repo_count(pool, "s1")


@pytest.mark.asyncio
async def test_diff_review_count_still_degrades_on_a_missing_table():
    from app.services.diff_review import _story_write_repo_count
    pool = _pool(error=asyncpg.UndefinedTableError("nope"))
    assert await _story_write_repo_count(pool, "s1") == 0


# ── write filtering + wire shape ──────────────────────────────────────────

def test_write_clones_excludes_read_only_context():
    """A read-only ('needs') clone gets no branch, node or PR, so anything that diffs,
    commits, pushes or scopes a plan must not see it."""
    clones = [_clone(name="target", is_write=True), _clone(name="context", is_write=False)]
    assert [c["repo_name"] for c in write_clones(clones)] == ["target"]


def test_bundle_shape_matches_the_gateway_snapshot():
    """Clients read either source with one code path, so the keys must not drift."""
    assert to_bundle_repos([_clone(subdir="repos/acme__svc-a")]) == [
        {"name": "acme/svc-a", "path": "repos/acme__svc-a",
         "branch": "feat/x", "write": True},
    ]


def test_bundle_shape_tolerates_a_missing_owner():
    """Legacy rows can carry an empty owner; the name must not become "/svc-a"."""
    assert to_bundle_repos([_clone(owner="")])[0]["name"] == "svc-a"
