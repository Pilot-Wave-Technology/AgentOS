"""The local-route HTTP harness works: one route, every kind of caller.

This is the harness every authorization test of the local lane stands on, so
its own behaviour is pinned first — a harness that let every token through
would make every later "refused" test meaningless.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


@pytest.mark.asyncio
async def test_the_story_token_for_this_story_reads_its_state():
    async with local_http() as h:
        goal, story = await h.seed_story()
        r = await h.post("/get-state", {"story_id": story}, token=h.story_token(story, goal))
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True


@pytest.mark.asyncio
async def test_a_story_token_for_another_story_is_refused():
    async with local_http() as h:
        goal, story = await h.seed_story()
        _, other = await h.seed_story(goal_id=goal)
        r = await h.post("/get-state", {"story_id": story}, token=h.story_token(other, goal))
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_no_token_and_an_expired_token_are_refused():
    async with local_http() as h:
        goal, story = await h.seed_story()
        assert (await h.post("/get-state", {"story_id": story})).status_code == 401
        r = await h.post("/get-state", {"story_id": story}, token=h.expired(story, goal))
        assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_a_human_route_refuses_the_story_token_and_accepts_a_member():
    async with local_http() as h:
        goal, story = await h.seed_story(phase="awaiting_plan_approval")
        body = {"story_id": story, "goal_id": goal}
        r = await h.post("/plan-reject", {**body, "feedback": "x"}, token=h.story_token(story, goal))
        assert r.status_code == 403, r.text
        r = await h.post("/plan-reject", {**body, "feedback": "x"}, token=h.human("stranger"))
        assert r.status_code == 403, r.text
        r = await h.post("/plan-reject", {**body, "feedback": "x"}, token=h.human("person"))
        assert r.status_code in (200, 400, 409), r.text   # reached the service


@pytest.mark.asyncio
async def test_the_harness_rolls_back_and_reads_the_ledger():
    async with local_http() as h:
        goal, story = await h.seed_story()
        assert (await h.story(story))["execution_mode"] == "local"
        assert await h.ledger(goal) == []
