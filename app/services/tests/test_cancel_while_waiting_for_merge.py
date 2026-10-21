"""A VS Code story can be cancelled while it waits for its merge, cleanly.

The wedge seen on feature: a story at `awaiting_merge` whose work was no longer
wanted had no way out -- the board showed no Cancel, and even through the API
its live branch survived the cancel (reconcile never touches a live branch), so
its PR could still be merged. For a LOCAL story, cancel now retires its nodes
and frees the checkout. A cloud story is untouched (rule zero).
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _cancel(goal, story):
    from app.routers.stories import _cancel_story
    return await _cancel_story(goal, story, "no longer needed", actor_uid="person")


@pytest.mark.asyncio
async def test_a_waiting_local_story_is_cancelled_its_branch_retired_and_its_checkout_freed():
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge", held_by="person")
        node = await h.seed_node(story, goal, state="MERGING", pr=29)
        r = await _cancel(goal, story)
        assert r.status_code == 200, r.body
        row = await h.story(story)
        assert row["state"] == "cancelled"
        assert row["checked_out_to_uid"] is None and row["checkout_session_id"] is None
        assert await h.conn.fetchval("SELECT state FROM git_nodes WHERE id = $1", node) == "ABANDONED"
        changes = [p for e, _, p in await h.ledger(goal) if e == "story.state_changed"]
        assert changes and changes[-1]["from_state"] == "awaiting_merge" and changes[-1]["to_state"] == "cancelled"


@pytest.mark.asyncio
async def test_an_already_merged_lane_is_left_alone():
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge", held_by="person")
        merged = await h.seed_node(story, goal, state="MERGED")
        await _cancel(goal, story)
        assert await h.conn.fetchval("SELECT state FROM git_nodes WHERE id = $1", merged) == "MERGED"


@pytest.mark.asyncio
async def test_a_cloud_story_waiting_for_its_merge_is_cancelled_exactly_as_before():
    """Rule zero: no new teardown for a non-local story; its live node is untouched."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge", mode="operator", held_by=None)
        node = await h.seed_node(story, goal, state="MERGING", pr=30)
        r = await _cancel(goal, story)
        assert r.status_code == 200
        assert await h.conn.fetchval("SELECT state FROM git_nodes WHERE id = $1", node) == "MERGING"


@pytest.mark.asyncio
async def test_a_working_local_story_can_be_cancelled_too():
    """It could not be, and that was this test. A local story mid-work
    is exactly the case somebody abandons and needs to stop -- and
    leaving it `in_progress` kept its checkout claimed, which costs
    the person their NEXT story as well as this one."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by="person")
        assert (await _cancel(goal, story)).status_code != 409
        row = await h.story(story)
        assert row["state"] == "cancelled"
        assert row["checked_out_to_uid"] is None, (
            "the checkout is still claimed by a cancelled story")


def test_the_board_offers_cancel_only_for_a_waiting_local_story():
    from jinja2 import Environment
    src = open("app/templates/stories/board.html", encoding="utf-8").read()
    start = src.index("{% if state not in ('done'")
    cond = src[start:src.index("%}", start) + 2]
    tmpl = Environment().from_string(cond + "YES{% else %}NO{% endif %}")
    render = lambda state, mode, kind="story": tmpl.render(state=state, story={"execution_mode": mode, "work_kind": kind})
    # THE CONDITION IS NOW ABOUT BEING UNFINISHED, not about the lane.
    # A cloud story waiting on its merge is offered Cancel for the same
    # reason a local one is: somebody has to be able to stop it.
    assert render("awaiting_merge", "local") == "YES"
    assert render("awaiting_merge", "operator") == "YES"
    assert render("in_progress", "operator") == "YES"
    for finished in ("done", "reviewed", "cancelled", "rejected"):
        assert render(finished, "local") == "NO", finished
    assert render("in_progress", "local") == "YES"
    assert render("accepted", "operator") == "YES"
