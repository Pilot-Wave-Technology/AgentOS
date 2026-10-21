"""A second cycle on a stale branch takes trunk in, or it does not submit.

THE GAP. The catch-up sweep offers a plan only to a story at
`awaiting_merge`. Asking for more work moves the story OFF that state --
it is being worked again -- so a second cycle on a branch that is behind
trunk gets no offer, and before this nothing else would make it take
trunk in. The work lands on a stale base, and the first anyone knows is
a conflict in the pull request or, worse, a clean merge of code written
against an API that has since changed.

ASKED OF GITHUB, NOT OF OUR OWN BOOKKEEPING, and that is the part worth
pinning. `last_synced_trunk_sha` looks like the answer: it is written by
the ENGINE, on a later tick, from its own compare. At submit time it
still reads "behind", so gating on it would refuse the very push that
fixes it -- a deadlock dressed as a guard.

AND NOT ON CYCLE 1, for the same shape of reason: a first cycle has no
catch-up route, so refusing it would strand the story.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.tests.local_http import local_http


async def _behind_story(h, *, cycle: int = 2):
    """A local story with a lane behind trunk, on cycle `cycle`."""
    goal, story = await h.seed_story(state="in_progress")
    await h.conn.execute(
        "UPDATE stories SET execution_mode = 'local' WHERE id = $1", story)
    node = await h.seed_node(story, goal, state="BRANCH_CREATED",
                             owner="acme", name="api", trunk="main")
    repo = await h.conn.fetchval(
        "SELECT git_repo_id FROM git_nodes WHERE id = $1", node)
    await h.conn.execute(
        "INSERT INTO git_repo_trunks (git_repo_id, trunk_branch, trunk_sha, "
        "   trunk_sha_seen_at) VALUES ($1, 'main', 'trunksha', "
        "   now() + interval '1 hour') "
        "ON CONFLICT (git_repo_id, trunk_branch) DO UPDATE "
        "   SET trunk_sha = EXCLUDED.trunk_sha", repo)
    await h.conn.execute(
        "UPDATE git_nodes SET base_sha = 'cutpoint' WHERE id = $1", node)
    for n in range(1, cycle + 1):
        await h.conn.execute(
            "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
            "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
            "   approved_by) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'work', 'p.md', "
            "        '{}'::jsonb, 'local', $3, now(), 'person')",
            story, n, "approved" if n == cycle else "executed")
    return goal, story, node


def _github(monkeypatch, behind_by: int):
    from app.services.git_graph import github_ops as gh
    monkeypatch.setattr(gh, "mint_repo_token", AsyncMock(return_value="t"))
    monkeypatch.setattr(gh, "compare",
                        AsyncMock(return_value={"behind_by": behind_by,
                                                "ahead_by": 1}))


# ── the flag ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_later_cycle_behind_trunk_is_told_to_take_it_in():
    from app.services.local_trunk_gate import take_in_trunk

    async with local_http() as h:
        _, story, _ = await _behind_story(h)
        assert await take_in_trunk(h.conn, story) is True


@pytest.mark.asyncio
async def test_a_first_cycle_is_not():
    """It has no catch-up route, and its base is the branch's cut point
    — as fresh as the story is."""
    from app.services.local_trunk_gate import take_in_trunk

    async with local_http() as h:
        _, story, _ = await _behind_story(h, cycle=1)
        assert await take_in_trunk(h.conn, story) is False


@pytest.mark.asyncio
async def test_a_cycle_whose_lanes_are_level_is_not():
    from app.services.local_trunk_gate import take_in_trunk

    async with local_http() as h:
        _, story, node = await _behind_story(h)
        await h.conn.execute(
            "UPDATE git_nodes SET last_synced_trunk_sha = 'trunksha' "
            " WHERE id = $1", node)
        assert await take_in_trunk(h.conn, story) is False


@pytest.mark.asyncio
async def test_get_state_carries_the_flag():
    async with local_http() as h:
        goal, story, _ = await _behind_story(h)
        r = await h.post("/get-state", {"story_id": story},
                         token=h.story_token(story, goal))
        assert r.json()["take_in_trunk"] is True


# ── the gate ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_commit_without_trunk_in_it_is_refused(monkeypatch):
    from app.services.local_trunk_gate import refusal

    async with local_http() as h:
        _, story, _ = await _behind_story(h)
        _github(monkeypatch, behind_by=3)
        r = await refusal(h.conn, story, "newsha")
        assert r and r["reason"] == "trunk_not_taken_in"
        assert "acme/api" in r["error"], "the refusal should name the lane"


@pytest.mark.asyncio
async def test_a_commit_containing_trunk_passes(monkeypatch):
    from app.services.local_trunk_gate import refusal

    async with local_http() as h:
        _, story, _ = await _behind_story(h)
        _github(monkeypatch, behind_by=0)
        assert await refusal(h.conn, story, "newsha") is None


@pytest.mark.asyncio
async def test_the_question_goes_to_github_not_to_our_own_column(monkeypatch):
    """THE DEADLOCK THIS AVOIDS. `last_synced_trunk_sha` is written by
    the engine on a LATER tick: at submit time it still says behind, so
    a gate reading it would refuse the push that fixes it."""
    from app.services.local_trunk_gate import refusal

    async with local_http() as h:
        _, story, node = await _behind_story(h)
        _github(monkeypatch, behind_by=0)
        stale = await h.conn.fetchval(
            "SELECT COALESCE(last_synced_trunk_sha, base_sha) FROM git_nodes "
            " WHERE id = $1", node)
        assert stale != "trunksha", "the fixture no longer models the race"
        assert await refusal(h.conn, story, "newsha") is None, (
            "the gate read our own bookkeeping and refused a commit that "
            "does contain trunk")


@pytest.mark.asyncio
async def test_a_github_failure_fails_open(monkeypatch):
    """A staleness guard, not a security boundary — the pull request
    still exists and a person still merges it. An outage that blocked
    every local submit would be the worse failure."""
    from app.services.git_graph import github_ops as gh
    from app.services.local_trunk_gate import refusal

    async with local_http() as h:
        _, story, _ = await _behind_story(h)
        monkeypatch.setattr(gh, "mint_repo_token",
                            AsyncMock(side_effect=RuntimeError("502")))
        assert await refusal(h.conn, story, "newsha") is None


@pytest.mark.asyncio
async def test_no_commit_sha_is_not_a_refusal(monkeypatch):
    from app.services.local_trunk_gate import refusal

    async with local_http() as h:
        _, story, _ = await _behind_story(h)
        _github(monkeypatch, behind_by=5)
        assert await refusal(h.conn, story, "") is None
