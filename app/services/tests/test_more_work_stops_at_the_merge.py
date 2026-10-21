"""More work is asked for BEFORE the merge, in both lanes.

ABHISHEK, 2026-10-14: "more work should always be done before merging
-- that is how work happens everywhere." The merge is the commitment.
Asking afterwards reopens work somebody has already accepted onto
trunk.

IT WAS ALSO THE ONLY PATH THAT NEEDED A MERGED BRANCH BROUGHT BACK TO
LIFE, and that is where it broke, twice, on ec6d5d1f. The node is
reset, the create pass re-stamps `base_sha` with the live trunk tip,
and the branch -- never actually re-cut -- sits behind by its own
merge commit while the bookkeeping calls it level. The grind pass asks
GitHub, sees a behind lane and opens no pull request; the catch-up
sweep asks the cache, sees "level" and offers nothing. The work sat on
the branch with nothing pointing at it.

Refusing is not a way around that. It is the rule the mechanism was
built to bend.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _thread(h, goal: str, story: str) -> None:
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story, tid)


async def _spent(h, story: str) -> None:
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
        "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
        "   approved_by) "
        "VALUES (gen_random_uuid(), $1, 1, 1, 'work', 'p.md', '{}'::jsonb, "
        "        'local', 'executed', now(), 'person')", story)


async def _ask(h, goal: str, story: str):
    from app.services.local_more_work import request_more_work
    return await request_more_work(goal, story, "one more thing", "u1")


# ── local ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_fully_merged_story_is_refused():
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _spent(h, story)
        await h.seed_node(story, goal, state="MERGED", pr=1)
        r = await _ask(h, goal, story)
        assert r["ok"] is False
        assert r["reason"] == "already_merged"
        assert "BEFORE merging" in r["error"], (
            "the refusal should say when it WOULD have worked")


@pytest.mark.asyncio
async def test_before_the_merge_it_still_works():
    """The rule is about the merge, not about having a branch."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _spent(h, story)
        await h.seed_node(story, goal, state="MERGING", pr=1)
        assert (await _ask(h, goal, story))["ok"] is True


@pytest.mark.asyncio
async def test_one_merged_lane_of_two_is_not_the_end():
    """A multi-repo story merges one repo at a time. The story is over
    when ALL of its work has landed, not when the first repo does."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _spent(h, story)
        await h.seed_node(story, goal, state="MERGED", name="api", pr=1)
        await h.seed_node(story, goal, state="MERGING", name="web", pr=2)
        assert (await _ask(h, goal, story))["ok"] is True


@pytest.mark.asyncio
async def test_a_story_with_no_branch_can_always_take_more():
    """A docs story or an investigation has nothing to have merged, and
    reading "no lanes" as "all landed" would close it for ever."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress")
        await _thread(h, goal, story)
        await _spent(h, story)
        assert (await _ask(h, goal, story))["ok"] is True


@pytest.mark.asyncio
async def test_a_story_whose_lanes_were_all_ABANDONED_is_not_finished():
    """Abandoning IS the decision about a lane -- but a story that
    landed nothing has not delivered, and must be able to try again."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress")
        await _thread(h, goal, story)
        await _spent(h, story)
        await h.seed_node(story, goal, state="ABANDONED")
        assert (await _ask(h, goal, story))["ok"] is True


@pytest.mark.asyncio
async def test_an_errand_is_unaffected():
    """It has no landing at all, so "after the merge" cannot be true
    of one."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", work_kind="errand")
        await _thread(h, goal, story)
        await h.seed_node(story, goal, state="MERGED", pr=1)
        assert (await _ask(h, goal, story))["ok"] is True


# ── cloud ────────────────────────────────────────────────────────────

def test_the_cloud_offers_it_only_at_the_LANDING_park():
    from app.services.kernel_more_work import (CLOSEOUT_KIND, LANDING_KIND,
                                               WAITING_KINDS)

    assert WAITING_KINDS == (LANDING_KIND,)
    assert CLOSEOUT_KIND not in WAITING_KINDS, (
        "the closeout park is AFTER the merge; offering more work there "
        "reopens work already accepted onto trunk")


def test_the_closeout_card_offers_only_done():
    """The OTHER door. Turning the button off leaves the card, and
    answering it `revise` drives the same reopen through
    `story_landing.sweep_resumed`."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "kernel_park_cards.py").read_text(encoding="utf-8")
    i = src.index('elif row["kind"] == "closeout":')
    block = src[i:src.index('elif row["kind"] == "pr_closed":', i)]
    assert '"id": "approve"' in block
    assert '"id": "revise"' not in block, (
        "the closeout card still offers more work")


def test_the_landing_card_still_offers_it():
    """Before the merge is exactly where it belongs."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "kernel_park_cards.py").read_text(encoding="utf-8")
    i = src.index('options = [\n            {"id": "revise"')
    assert i > 0, "the landing card no longer offers more work"


def test_the_closeout_body_no_longer_advertises_it():
    """A card whose text sells an option it does not have is worse than
    one that never mentioned it."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "kernel_park_cards.py").read_text(encoding="utf-8")
    i = src.index("def _closeout_body(")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "asking for it now continues THIS story" not in body
    assert "BEFORE the merge" in body, (
        "it should say when more work SHOULD have been asked for")
