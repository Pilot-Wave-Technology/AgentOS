"""`get-state`'s `landing` block — the contract the panel is built on.

WITH NO DIFF-REVIEW CARD, THE PULL REQUEST IS THE REVIEW. The panel can
only point a person at one if the control plane says where it is, so
this block is the whole of what the extension knows about where the
work went. Pinned as a contract because it is a cross-repo coupling:
the extension's story rows read these keys by name, and a rename here
is a blank panel there rather than a failing test.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _state(h, goal: str, story: str) -> dict:
    r = await h.post("/get-state", {"story_id": story},
                     token=h.story_token(story, goal))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_the_block_is_always_present_even_with_no_lanes():
    """A client that has to test for the key's existence before reading
    it will one day forget. Absent is not a value."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        landing = (await _state(h, goal, story))["landing"]
        assert landing == {"prs": [], "all_merged": False, "waiting_on": []}


@pytest.mark.asyncio
async def test_a_pushed_lane_carries_everything_needed_to_show_a_row():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await h.seed_node(story, goal, state="MERGING", owner="acme",
                          name="api", trunk="main", pr=12)
        [pr] = (await _state(h, goal, story))["landing"]["prs"]
        assert pr["repo"] == "acme/api"
        assert pr["lane"] == "main"
        assert pr["number"] == 12
        assert pr["url"].endswith("/acme/api/pull/12")
        assert pr["state"] == "MERGING"
        # The workspace mount, so the boundary hook can tell which lanes
        # are writable while the story is integrating without a network
        # call of its own.
        assert pr["mount"] == "repos/acme__api"


@pytest.mark.asyncio
async def test_a_lane_with_no_pr_has_an_empty_url_not_a_broken_one():
    """A row linking to `/pull/None` is worse than a row that says there
    is nothing to open yet."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await h.seed_node(story, goal, state="BRANCH_CREATED", pr=None)
        [pr] = (await _state(h, goal, story))["landing"]["prs"]
        assert pr["number"] is None
        assert pr["url"] == ""


@pytest.mark.asyncio
async def test_merged_lanes_stay_in_the_list():
    """A list that empties as lanes merge reads as work disappearing.
    The person wants to see the ones already in."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await h.seed_node(story, goal, state="MERGED", name="api", pr=1)
        await h.seed_node(story, goal, state="MERGING", name="web", pr=2)
        landing = (await _state(h, goal, story))["landing"]
        assert len(landing["prs"]) == 2, "a merged lane was dropped from the list"
        # Only the unmerged one is being waited on, and it is named by the
        # branch the person would go and merge.
        assert len(landing["waiting_on"]) == 1
        merging = next(p for p in landing["prs"] if p["state"] == "MERGING")
        assert landing["waiting_on"] == [merging["branch"]]


@pytest.mark.asyncio
async def test_all_merged_is_true_only_when_every_lane_is_in():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await h.seed_node(story, goal, state="MERGED", name="api", pr=1)
        assert (await _state(h, goal, story))["landing"]["all_merged"] is True

        await h.seed_node(story, goal, state="MERGING", name="web", pr=2)
        assert (await _state(h, goal, story))["landing"]["all_merged"] is False


@pytest.mark.asyncio
async def test_an_abandoned_lane_does_not_hold_all_merged_open():
    """Abandoning a lane IS the decision about it. Counting it as
    outstanding would keep Mark Done greyed out for ever."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await h.seed_node(story, goal, state="MERGED", name="api", pr=1)
        await h.seed_node(story, goal, state="ABANDONED", name="web")
        assert (await _state(h, goal, story))["landing"]["all_merged"] is True


@pytest.mark.asyncio
async def test_all_merged_is_false_with_no_lanes_and_that_is_not_the_gate():
    """It means "every lane is in", and a story with nothing to land has
    not landed anything. Mark Done must still be reachable for it, which
    is `story_done_blocker`'s job and not this flag's."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        _, story = await h.seed_story()
        await h.conn.execute(
            "INSERT INTO plan_versions (id, story_id, version, plan_doc_path, "
            "   plan_json, proposed_by, status, approved_at, approved_by) "
            "VALUES (gen_random_uuid(), $1, 1, 'p.md', '{}'::jsonb, 'local', "
            "        'approved', now(), 'person')", story)
        assert await story_done_blocker(story) is None
