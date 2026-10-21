"""An errand produces an answer. It does not get to push.

`profiles-local/errand-ops` gives an errand a `git` workspace driver, because
an ops errand is almost always ABOUT a repository -- read the config, run the
script, check what came back. What it must not have is a way to land commits
on one.

Before this, NOTHING in the local lane looked at `work_kind`: an errand could
call `/internal/local/push-token`, receive a GitHub installation token with
`contents: write`, and push the branch. The only thing between it and a landed
commit was the prompt asking it not to -- and the shared CLAUDE.md actively
told it "the extension pushes the branch when you submit the diff", which for
an errand describes something that should never happen.

Two gates, and the first is the one that matters: the TOKEN is the capability.
Refusing only at diff-submit would still have handed out the credential.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.tests.test_local_workspace_nodes_sql import _node, _story
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
        await conn.close()


async def _as_errand(conn, sid: str):
    await conn.execute("UPDATE stories SET work_kind = 'errand' WHERE id = $1", sid)


@contextlib.contextmanager
def _pool_is(conn):
    from app.services import local_diff
    orig = local_diff.get_pool
    local_diff.get_pool = lambda: conn
    try:
        yield
    finally:
        local_diff.get_pool = orig


@pytest.mark.asyncio
async def test_an_errand_cannot_mint_a_push_token():
    from app.services.local_diff import mint_push_token

    async with _conn() as conn:
        sid = f"{PREFIX}-errand-token"
        await _story(conn, sid)
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        await _as_errand(conn, sid)
        with _pool_is(conn):
            result = await mint_push_token(sid)
    assert result["ok"] is False
    assert result["reason"] == "errand_no_push"
    assert "token" not in result, (
        "a refusal that still carries a token refuses nothing"
    )


@pytest.mark.asyncio
async def test_an_errand_cannot_submit_a_diff():
    from app.services.local_diff import submit_local_diff

    async with _conn() as conn:
        sid = f"{PREFIX}-errand-diff"
        await _story(conn, sid)
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        await _as_errand(conn, sid)
        with _pool_is(conn):
            result = await submit_local_diff(sid, "diff --git a b", "1 file", False, "abc123")
    assert result["ok"] is False
    assert result["reason"] == "errand_no_push", (
        "a person with their own git credentials can push by hand; accepting the "
        "diff would then open a review card proposing to land an errand's commits"
    )


@pytest.mark.asyncio
async def test_a_story_is_unaffected():
    # The gate must key on work_kind and nothing else -- a coding story still
    # reaches the real token path (and fails there only for want of a GitHub App,
    # which is a different refusal).
    from app.services.local_diff import mint_push_token

    async with _conn() as conn:
        sid = f"{PREFIX}-story-token"
        await _story(conn, sid)
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        with _pool_is(conn):
            result = await mint_push_token(sid)
    assert result.get("reason") != "errand_no_push", (
        "the errand gate is catching coding stories"
    )


def test_the_errand_prompt_says_so_in_the_same_words():
    # A refusal the agent was never warned about reads as a broken tool.
    from app.services.local_bundle import ERRAND_DELIVERY, STORY_DELIVERY, _delivery_section

    assert _delivery_section("errand") is ERRAND_DELIVERY
    assert _delivery_section("story") is STORY_DELIVERY
    assert "nothing to push and nothing to submit" in ERRAND_DELIVERY
    assert "refuses to mint a push token" in ERRAND_DELIVERY, (
        "the prompt should say the refusal is enforced, not merely requested"
    )
    assert "the extension pushes the branch" not in ERRAND_DELIVERY, (
        "the errand prompt describes a landing that cannot happen"
    )
    assert "the extension pushes the branch" in STORY_DELIVERY, (
        "the story prompt lost the one instruction that keeps a session from "
        "pushing by hand"
    )


@pytest.mark.asyncio
async def test_a_refused_submission_does_not_park_the_story_for_review():
    """`awaiting_review` is what the panel gates "Review & Approve Diff" on.

    Parking on a submission that was REFUSED would leave the story waiting for
    a review of a diff nobody accepted, with the button offering to approve
    nothing.
    """
    from app.services.local_diff import submit_local_diff

    async with _conn() as conn:
        sid = f"{PREFIX}-noprk"
        await _story(conn, sid)
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        await _as_errand(conn, sid)
        before = await conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", sid)
        with _pool_is(conn):
            r = await submit_local_diff(sid, "diff", "1 file", False, "sha")
        after = await conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", sid)
    assert r["ok"] is False
    assert after == before, (
        "a refused submission parked the story for a review that cannot happen"
    )


def test_a_successful_submission_parks_the_story_for_review():
    """A submitted story must LEAVE `executing`, whatever the next state
    is called.

    This used to pin `awaiting_review`, because nothing set it: the DAG
    handler that did went with the DAG, so a local story stayed in
    `executing` after Submit Diff and the snapshot kept telling the session
    to do the work it had just submitted.

    The state is now `awaiting_merge` -- there is no diff review in this
    lane, and the thing being waited on is a person merging the pull
    request. The failure this guards against is unchanged: a submitted
    story that still looks like it is being worked on.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[3]
           / "app" / "services" / "local_diff.py").read_text(encoding="utf-8")
    assert "state = 'awaiting_merge'" in src, (
        "a submitted diff leaves the story in `executing`, so nothing "
        "downstream can tell it is finished"
    )
    # Both submit paths, single-repo and multi-repo.
    assert src.count("_park_for_review(pool, story_id, await post_diff_review(") == 2
