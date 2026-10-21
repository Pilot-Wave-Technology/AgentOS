"""S3: only the person a story is assigned to can check it out.

The `uid` came from the request body and nothing compared it with
`assigned_to`, so a holder of a token for the story could take the lock in
somebody else's name.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


@pytest.mark.asyncio
async def test_a_uid_that_is_not_the_assignee_is_refused_and_nothing_changes():
    from app.services.local_checkout import claim_checkout
    async with local_http() as h:
        goal, story = await h.seed_story(state="accepted", held_by=None)
        r = await claim_checkout(story, goal, "someone-else")
        assert r["ok"] is False and r["reason"] == "not_assignee"
        row = await h.story(story)
        assert row["checked_out_to_uid"] is None and row["checkout_session_id"] is None


@pytest.mark.asyncio
async def test_an_unassigned_story_cannot_be_checked_out():
    from app.services.local_checkout import claim_checkout
    async with local_http() as h:
        goal, story = await h.seed_story(state="accepted", held_by=None, assigned_to="")
        assert (await claim_checkout(story, goal, "person"))["reason"] == "not_assignee"


@pytest.mark.asyncio
async def test_over_http_the_body_uid_must_be_the_assignee():
    async with local_http() as h:
        goal, story = await h.seed_story(state="accepted", held_by=None)
        r = await h.post("/checkout", {"story_id": story, "goal_id": goal, "uid": "intruder"},
                         token=h.story_token(story, goal))
        assert r.status_code == 409
        assert r.json()["reason"] == "not_assignee"


@pytest.mark.asyncio
async def test_the_assignee_gets_past_the_door():
    """The assignee is not refused for WHO they are; any later refusal is about the
    story's readiness, which has its own tests."""
    from app.services.local_checkout import claim_checkout
    async with local_http() as h:
        goal, story = await h.seed_story(state="accepted", held_by=None)
        r = await claim_checkout(story, goal, "person")
        assert r.get("reason") != "not_assignee", r
