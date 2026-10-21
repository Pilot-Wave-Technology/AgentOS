"""`post_diff_review` must work for a caller that has no per-repo breakdown.

Regression guard for a P0 found in audit (2026-08-18, see
`research/LOCAL_EXEC_MULTIREPO_INTEGRATION.md`): the multi-repo landing
(`443ac50`) added a `per_repo` list to `begin_diff_review` and then referenced
that name from inside `post_diff_review` — a DIFFERENT function, which took no
such parameter and had no module-level fallback. Line `"repos": per_repo` sat in
an unconditionally-built payload dict, so EVERY call raised
`NameError: name 'per_repo' is not defined`.

Why that was so damaging: `post_diff_review` is the single approval gate shared
by both execution modes — `begin_diff_review` (gateway) and
`local_diff.submit_local_diff` (the VS Code / local-execution path, which reuses
it deliberately). No feature flag guarded it, so diff review was dead for every
story on the branch, in a deployed environment. `origin/main` never had the bug.

It survived a 17-defect review because NOTHING called `post_diff_review` in a
test. These tests pin the CONTRACT — that the function is callable with no repo
breakdown and that `per_repo` is a real parameter — rather than any one call
site, so the guarantee survives future callers being added.
"""
from __future__ import annotations

import inspect
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.services import diff_review as dr


class _FakeConn:
    """Minimal asyncpg-conn stand-in: only `transaction()` is exercised."""

    def transaction(self):
        return _AsyncNull()


class _AsyncNull:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """Records every `execute` so the built payload/card can be inspected."""

    def __init__(self):
        self.execute = AsyncMock()

    def acquire(self):
        return _FakeAcquire()

    def sql_arg(self, needle: str):
        """Args of the first execute whose SQL mentions `needle`, else None."""
        for call in self.execute.await_args_list:
            if needle in call.args[0]:
                return call.args
        return None


def _patched(pool):
    """Patch out the DB, the ledger, and actor resolution — leaving only the
    payload/card construction under test. The fan-out helpers
    (`notify_new_message`, Slack `relay_card`) are imported lazily inside
    try/except blocks and are already non-fatal, so they need no patching."""
    return [
        patch.object(dr, "get_pool", lambda: pool),
        patch.object(dr, "append_event", AsyncMock()),
        patch.object(dr, "instance_actor", AsyncMock(return_value=("actor-1", "agent"))),
    ]


async def _post(pool, **overrides):
    """Call post_diff_review with a valid local-path-shaped payload by default.

    `thread_id` is deliberately NON-empty: four of the five original `per_repo`
    references lived in the card-building branch, which is skipped entirely when
    there is no thread. A test without a thread id would not have caught the bug.
    """
    kwargs = {
        "goal_id": "goal-1",
        "story_id": "story-1",
        "dag_task_id": "task-1",
        "thread_id": "thread-1",
        "diff_text": "diff --git a/x b/x\n+one line\n",
        "stat": "1 file changed, 1 insertion(+)",
        "truncated": False,
        "diff_ref": "diff --git a/x b/x\n+one line\n",
        "commit_sha": "abc1234",
    }
    kwargs.update(overrides)
    with ExitStack() as stack:
        for manager in _patched(pool):
            stack.enter_context(manager)
        return await dr.post_diff_review(**kwargs)


@pytest.mark.asyncio
async def test_local_path_omitting_per_repo_does_not_raise():
    """The exact shape `submit_local_diff` sends: no `per_repo` at all.

    This is the call that raised NameError. It must succeed and record an empty
    repo list, matching pre-multi-repo behaviour on `main`.
    """
    pool = _FakePool()
    result = await _post(pool)

    assert result["ok"] is True
    args = pool.sql_arg("approvals")
    assert args is not None, "no INSERT into approvals was issued"
    payload = json.loads(args[3])
    assert payload["repos"] == [], "absent per_repo must normalise to an empty list"
    assert payload["commit_sha"] == "abc1234"


@pytest.mark.asyncio
async def test_gateway_single_repo_card_has_no_multi_repo_wording():
    """A single-repo gateway story (diff_path set, no per_repo) must get the
    plain card — byte-identical wording to the pre-multi-repo behaviour."""
    pool = _FakePool()
    await _post(pool, diff_path="docs/diff.patch", diff_ref="", commit_sha="")

    args = pool.sql_arg("messages")
    assert args is not None, "no card was posted"
    body = args[3]
    assert body.startswith("# Diff ready for review\n\n")
    assert "repositories" not in body
    card = json.loads(args[4])
    assert card["repos"] == []


@pytest.mark.asyncio
async def test_multi_repo_breakdown_is_rendered_when_supplied():
    """When a caller DOES pass per_repo, the multi-repo card must appear — the
    behaviour the multi-repo work intended, which the NameError masked."""
    pool = _FakePool()
    per_repo = [
        {"subdir": "svc-a", "stat": "2 files changed", "truncated": False},
        {"subdir": "svc-b", "stat": "1 file changed", "truncated": True},
    ]
    await _post(pool, diff_path="docs/diff.patch", diff_ref="", commit_sha="",
                per_repo=per_repo)

    args = pool.sql_arg("messages")
    body = args[3]
    assert "# Diff ready for review — 2 repositories" in body
    assert "`svc-a/`" in body and "`svc-b/`" in body
    assert "_(diff truncated)_" in body, "per-repo truncation flag must surface"

    approval = json.loads(pool.sql_arg("approvals")[3])
    assert approval["repos"] == per_repo


def test_per_repo_is_a_declared_parameter():
    """Contract pin. `per_repo` must be a PARAMETER, never read from an
    enclosing scope — that cross-function read was the original defect, and it
    would fail again the moment a second caller lacks the name."""
    params = inspect.signature(dr.post_diff_review).parameters
    assert "per_repo" in params, "per_repo must be an explicit parameter"
    assert params["per_repo"].default is None, (
        "per_repo must default to None so callers with no repo breakdown "
        "(the local-execution path) keep working"
    )
    assert params["per_repo"].kind is inspect.Parameter.KEYWORD_ONLY


def test_no_module_level_per_repo_fallback():
    """Guard against 'fixing' this by adding a module global, which would make
    one story's repo breakdown leak into the next story's approval."""
    assert not hasattr(dr, "per_repo"), (
        "a module-level per_repo would silence the NameError while introducing "
        "cross-call state leakage — keep it a parameter"
    )
