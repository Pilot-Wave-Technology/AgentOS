"""A local lane with its pull request open is still the story's to work.

THE CATCH-UP IS THE WHOLE REASON. A local story integrates ITSELF: trunk
moves, the person approves a catch-up plan, and the catch-up has to
hydrate the clone and push the result -- to the same branch, updating
the same pull request. A node in `QUEUED`, `MERGING` or `CONFLICTED` was
excluded from both, so hydrate answered `no_repo` and the push token was
refused, on a story whose entire remaining job is to push.

`QUEUED` IS NOT A CORNER CASE. `enqueue_ready_merges` moves every READY
node there on an ordinary engine tick, so this is where a local story's
lane spends most of its wait.

AND THE POD LANE IS UNCHANGED, which is the other half. Its branches are
integrated by somebody else; a pod lane with a PR open genuinely has
nothing left for its own story to do, and serving it a clone would
invite work nobody can land.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http

PR_OPEN = ["QUEUED", "MERGING", "CONFLICTED"]


async def _story_with_lane(h, state: str, *, local: bool = True):
    goal, story = await h.seed_story(state="awaiting_merge")
    await h.conn.execute(
        "UPDATE stories SET execution_mode = $2 WHERE id = $1",
        story, "local" if local else "operator")
    await h.seed_node(story, goal, state=state)
    return goal, story


# ── hydrate ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("state", PR_OPEN)
@pytest.mark.asyncio
async def test_a_local_lane_with_its_pr_open_still_resolves(state):
    from app.services.local_hydrate import _resolve_live_repos

    async with local_http() as h:
        _, story = await _story_with_lane(h, state)
        repos = await _resolve_live_repos(h.conn, story)
        assert len(repos) == 1, (
            f"a local lane in {state} was not served — the catch-up it is "
            "waiting for cannot hydrate or push")


@pytest.mark.parametrize("state", PR_OPEN)
@pytest.mark.asyncio
async def test_a_POD_lane_in_the_same_state_is_not_served(state):
    """RULE ZERO: the pod lane's served set is unchanged."""
    from app.services.local_hydrate import _resolve_live_repos

    async with local_http() as h:
        _, story = await _story_with_lane(h, state, local=False)
        assert await _resolve_live_repos(h.conn, story) == []


@pytest.mark.asyncio
async def test_the_states_that_were_always_served_still_are():
    from app.services.local_hydrate import _resolve_live_repos

    async with local_http() as h:
        for mode in (True, False):
            _, story = await _story_with_lane(h, "BRANCH_CREATED", local=mode)
            assert len(await _resolve_live_repos(h.conn, story)) == 1


@pytest.mark.asyncio
async def test_an_ABANDONED_lane_is_never_served():
    """Giving up on the lane IS the decision. Nothing about closing the
    story out needs a tree nobody is going to land."""
    from app.services.local_hydrate import _resolve_live_repos

    async with local_http() as h:
        _, story = await _story_with_lane(h, "ABANDONED")
        assert await _resolve_live_repos(h.conn, story) == []


@pytest.mark.asyncio
async def test_a_MERGED_lane_IS_served_while_the_story_closes_out():
    """CHANGED 2026-10-14, and the old behaviour is why.

    "The branch is finished with, so serving a clone would invite work
    nobody can land" is wrong about the window between the last merge
    and `done`. In it the story still owes the in-session review (which
    reads what it wrote), the memory consolidation, and the person's own
    look at what landed -- all of which need the tree.

    On 66317ffc the lane merged, the story sat at `awaiting_closeout`,
    and the import failed with "the engine hasn't cut this story's
    feature branch yet" about a branch that had been cut, worked, pushed
    and merged. There was no way back in to finish the story.

    The "invite work" worry is answered at the right layer: the boundary
    hook refuses source edits in `awaiting_merge` and
    `awaiting_closeout`, so the clone cannot be written to."""
    from app.services.local_hydrate import _resolve_live_repos

    async with local_http() as h:
        _, story = await _story_with_lane(h, "MERGED")
        assert len(await _resolve_live_repos(h.conn, story)) == 1


@pytest.mark.asyncio
async def test_a_MERGED_lane_is_NOT_served_once_the_story_is_finished():
    """`done` and `reviewed` genuinely have nothing left to do, and the
    checkout is meant to be released."""
    from app.services.local_hydrate import _resolve_live_repos

    async with local_http() as h:
        _, story = await _story_with_lane(h, "MERGED")
        for finished in ("done", "reviewed", "cancelled"):
            await h.conn.execute(
                "UPDATE stories SET state = $2 WHERE id = $1", story, finished)
            assert await _resolve_live_repos(h.conn, story) == [], finished


@pytest.mark.asyncio
async def test_a_POD_story_never_gets_its_merged_lane():
    """RULE ZERO. Its workspace is collected when the run ends; there is
    no session left to review anything."""
    from app.services.local_hydrate import _resolve_live_repos

    async with local_http() as h:
        _, story = await _story_with_lane(h, "MERGED", local=False)
        assert await _resolve_live_repos(h.conn, story) == []


# ── push ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state", PR_OPEN)
@pytest.mark.asyncio
async def test_the_push_token_is_minted_for_an_open_pr_lane(state, monkeypatch):
    """The token IS the capability — a refusal here is the catch-up
    unable to deliver, not merely inconvenienced."""
    from app.services import local_diff

    async with local_http() as h:
        _, story = await _story_with_lane(h, state)

        async def _tok(repos, permissions=None):
            assert permissions == {"contents": "write", "metadata": "read"}
            return {"token": "t", "expires_at": ""}

        monkeypatch.setattr(local_diff, "generate_installation_token", _tok)
        monkeypatch.setattr(local_diff, "get_pool", lambda: h.conn)
        r = await local_diff.mint_push_token(story)
        assert r["ok"] is True, r


@pytest.mark.asyncio
async def test_an_errand_is_still_refused_whatever_its_lane_is_doing(monkeypatch):
    """The widening is about WHICH states are live, not about who may
    push. An errand's work never lands."""
    from app.services import local_diff

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", work_kind="errand")
        await h.conn.execute(
            "UPDATE stories SET execution_mode = 'local' WHERE id = $1", story)
        await h.seed_node(story, goal, state="QUEUED")
        monkeypatch.setattr(local_diff, "get_pool", lambda: h.conn)
        r = await local_diff.mint_push_token(story)
        assert r["ok"] is False and r["reason"] == "errand_no_push"


# ── and the widening is spelled out once ─────────────────────────────

def test_the_widened_set_is_local_only_at_one_place():
    """Two copies of "which states count" is how the hydrate and the
    push token come to disagree, which reads as an intermittent
    `no_repo` on the half that was not updated."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "local_hydrate.py").read_text(encoding="utf-8")
    assert src.count("_PR_OPEN_STATES") == 2, (
        "the PR-open states are named more than once; they are defined "
        "and used, and nothing else")
    assert "list(_BRANCH_LIVE_STATES)," not in src, (
        "a query still passes the narrow list directly instead of asking "
        "`_served_states`")
