"""A local push is verified, not asserted.

The extension commits and pushes on the user's machine and then tells the control plane
the sha — the CP never sees that push happen. Approval used to take it on trust, so a push
that silently failed (expired token, protected branch, connection dropped after the
commit) produced a story marked done with nothing on the remote.

Two rules, and the second is the one that is easy to get wrong:

  1. A commit that is definitively NOT on the remote is refused at submit, so a phantom
     push never reaches a human for approval.
  2. A commit we could not CHECK is accepted, because an unreachable GitHub is not a failed
     push — but it must not be reported as a confirmed landing either.

Rule 2 must not be implemented by lowering `pushed`. On the gateway path `pushed=False`
means "commit landed, push failed, click Approve to retry", which leaves the approval
pending and returns an error. The CP performs no local push, so there is nothing to retry:
routing an unverified local push down that branch would make the story permanently
unapprovable with advice that cannot work.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import local_diff as ld


_REPO = {"owner": "acme", "repo": "svc-a", "branch": "feat/x",
         "clone_url": "u", "trunk": "main"}


def _pool(work_kind: str = "story"):
    """Pool where the story exists and no undecided approval is outstanding.

    KEYED ON THE QUERY, not on call order. This was `side_effect=[...]`, a fixed
    sequence, so inserting ANY new fetchval into `submit_local_diff` silently
    shifted every later answer by one and failed these four tests for a reason
    that had nothing to do with what they test -- which is exactly what happened
    when the errand push gate added its `work_kind` lookup. A fake that answers
    by what was asked survives the next insertion too.
    """
    async def fetchval(sql, *args, **kwargs):
        if "work_kind" in sql:
            return work_kind
        if "plan_versions" in sql:
            return 1               # an approved plan exists
        return None                # dedup miss, and anything else

    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={"goal_id": "g1", "active_thread_id": "t1"})
    pool.fetchval = AsyncMock(side_effect=fetchval)
    # A real pool hands out connections; `submit_local_diff` takes its per-story
    # submit lock on one (S4), so the fake must be able to as well.
    lock_conn = AsyncMock()

    class _Acquired:
        async def __aenter__(self):
            return lock_conn

        async def __aexit__(self, *exc):
            return False
    pool.acquire = lambda: _Acquired()
    return pool


async def _submit(verify_result, *, repo=_REPO, work_kind="story"):
    pool = _pool(work_kind)
    with patch.object(ld, "get_pool", lambda: pool), \
         patch.object(ld, "_repo_ref", AsyncMock(return_value=repo)), \
         patch.object(ld, "verify_commit_pushed", AsyncMock(return_value=verify_result)), \
         patch.object(ld, "post_diff_review", AsyncMock(return_value={"ok": True})) as post:
        result = await ld.submit_local_diff(
            "story-1", "diff --git a/x b/x\n+1\n", "1 file", False, "abc1234def",
        )
    return result, post


@pytest.mark.asyncio
async def test_a_commit_not_on_the_remote_is_refused():
    """The phantom push. Must never reach a human for approval."""
    result, post = await _submit(("missing", "branch or commit not found on the remote"))
    assert result["ok"] is False
    assert result["reason"] == "push_not_on_remote"
    assert "abc1234def"[:12] in result["error"]
    assert "protected" in result["error"], "the error should name a plausible cause"
    post.assert_not_awaited(), "no approval may be created for a push that did not land"


@pytest.mark.asyncio
async def test_a_confirmed_push_is_recorded_as_verified():
    result, post = await _submit(("verified", "identical"))
    assert result["ok"] is True
    assert post.await_args.kwargs["push_verified"] is True


@pytest.mark.asyncio
async def test_an_unreachable_remote_is_accepted_but_marked_unverified():
    """An outage is not a failed push, so the submission proceeds — but the flag must
    record that nothing was observed."""
    result, post = await _submit(("unknown", "compare failed: HTTP 503"))
    assert result["ok"] is True
    assert post.await_args.kwargs["push_verified"] is False


@pytest.mark.asyncio
async def test_verification_is_skipped_when_the_branch_is_unresolvable():
    """No repo ref means no branch to check against; the submit still proceeds rather
    than failing on a check it cannot perform."""
    result, post = await _submit(("missing", "n/a"), repo=None)
    assert result["ok"] is True
    assert post.await_args.kwargs["push_verified"] is None


# ── the deadlock that lowering `pushed` would have caused ──────────────────

def test_approval_does_not_route_an_unverified_local_push_into_the_retry_branch():
    """Regression guard for a deadlock, pinned by reading the source.

    `decide_diff_review` must keep `pushed = True` on the local branch. The retry branch
    it would otherwise reach tells the user to click Approve again — an instruction the CP
    cannot honour for a push it never performs, leaving the story unapprovable forever.
    """
    import inspect
    from app.services import diff_review

    src = inspect.getsource(diff_review.decide_diff_review)
    local_branch = src.split("if is_local:", 1)[1].split("else:", 1)[0]

    assert "pushed = True" in local_branch, (
        "the local branch must keep pushed=True; the verification result belongs in a "
        "separate flag, not in the variable that drives the retry branch"
    )
    assert "push_verified" in local_branch, "it must still READ the recorded verification"
    assert "pushed = bool(payload" not in local_branch, (
        "pushed must not be derived from the verification — that is the deadlock"
    )


def test_a_payload_without_the_flag_behaves_as_before():
    """Approvals created before verification existed have no `push_verified` key. They
    must keep working exactly as they did, so the default is True, not False."""
    import inspect
    from app.services import diff_review

    src = inspect.getsource(diff_review.decide_diff_review)
    assert 'payload.get("push_verified", True)' in src, (
        "absence means 'predates verification', not 'unverified'"
    )


# ── the verifier's own three-way mapping ──────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("compare_status,expected", [
    ("identical", "verified"),   # the branch tip IS the commit
    ("behind",    "verified"),   # the commit is an ancestor — something pushed after it
    ("ahead",     "missing"),    # the commit is not reachable from the branch
    ("diverged",  "missing"),
    ("",          "unknown"),    # unrecognised shape — never claim either way
])
async def test_compare_status_maps_to_the_right_verdict(compare_status, expected):
    """`behind` mapping to verified is the one that matters: a tip-equality check would
    call a perfectly good push bad as soon as anything else pushed to the branch."""
    from app import github_tokens as gt

    class _Resp:
        status_code = 200
        def json(self):  # noqa: D102
            return {"status": compare_status}

    class _TokResp:
        status_code = 201
        def json(self):  # noqa: D102
            return {"token": "ghs_x"}

    class _Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return _TokResp()
        async def get(self, *a, **k):
            return _Resp()

    with patch.object(gt, "_make_app_jwt", lambda: "jwt"), \
         patch.object(gt.httpx, "AsyncClient", _Client), \
         patch.object(gt, "get_settings", lambda: type("S", (), {"github_app_installation_id": "1"})()):
        status, _ = await gt.verify_commit_pushed("acme/svc-a", "feat/x", "abc")
    assert status == expected


@pytest.mark.asyncio
async def test_a_404_is_definitive_but_a_transport_error_is_not():
    """404 means the remote answered and the branch/commit is not there. A raised
    exception means we never got an answer — those must not collapse together."""
    from app import github_tokens as gt

    class _C404:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return type("T", (), {"status_code": 201, "json": lambda s: {"token": "t"}})()
        async def get(self, *a, **k): return type("R", (), {"status_code": 404, "text": "nope"})()

    class _CBoom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise ConnectionError("dns")
        async def get(self, *a, **k): raise ConnectionError("dns")

    settings = lambda: type("S", (), {"github_app_installation_id": "1"})()
    with patch.object(gt, "_make_app_jwt", lambda: "jwt"), patch.object(gt, "get_settings", settings):
        with patch.object(gt.httpx, "AsyncClient", _C404):
            assert (await gt.verify_commit_pushed("a/b", "br", "sha"))[0] == "missing"
        with patch.object(gt.httpx, "AsyncClient", _CBoom):
            assert (await gt.verify_commit_pushed("a/b", "br", "sha"))[0] == "unknown"


@pytest.mark.asyncio
async def test_missing_arguments_are_unknown_not_missing():
    """Absent inputs mean we cannot check, which must never read as 'the push failed'."""
    from app.github_tokens import verify_commit_pushed
    for args in [("", "br", "sha"), ("a/b", "", "sha"), ("a/b", "br", "")]:
        assert (await verify_commit_pushed(*args))[0] == "unknown"


@pytest.mark.asyncio
async def test_an_errand_is_refused_before_any_of_this():
    """The errand gate sits in the same function, and must not be reachable past
    it: an errand produces an answer, so there is no diff to land and no push to
    verify."""
    result, post = await _submit(("verified", "identical"), work_kind="errand")
    assert result["ok"] is False
    assert result["reason"] == "errand_no_push"
    post.assert_not_awaited()
