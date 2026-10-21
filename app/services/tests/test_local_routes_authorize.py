"""Every `/internal/local/*` route refuses the callers it must refuse — over HTTP.

Until this file no test called these routes at all, so a route that lost its
guard would have passed every test. Two kinds of route:

  STORY routes  -- carry the story-scoped token the session holds. Refused: no
                   token, an expired one, and a token for a DIFFERENT story.
  HUMAN routes  -- decisions (approve, done, recover...). Refused: the story
                   token (the agent must not decide), a person who is not in the
                   goal, and a story named through a goal it does not belong to.

Refusals happen before any work, so nothing here reaches GitHub or the gateway.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http

STORY_ROUTES = [
    "/checkout", "/heartbeat", "/complete", "/hydrate", "/github-token", "/profile",
    "/memory-hydrate", "/memory-writeback", "/plan-submit", "/done-check", "/story-review",
    "/recent-messages", "/get-state", "/report-message", "/push-token", "/diff-submit",
    "/artifact-emit", "/artifact-list",
]
HUMAN_ROUTES = [
    "/plan-approve", "/plan-reject", "/story-done", "/continue-plan", "/more-work",
    "/story-recover", "/diff-decide",
]


def _body(story: str, goal: str) -> dict:
    """Every field any local route requires, so a refusal is about WHO, not shape."""
    return {
        "story_id": story, "goal_id": goal, "uid": "person", "checkout_session_id": "sess-1",
        "role": "agent", "content": "x", "steps": ["x"], "files": {}, "decision": "approve",
        "action": "retry", "feedback": "", "what": "more", "verdict": "approve",
        "summary": "s", "diff": "d", "stat": "", "commit_sha": "", "role_name": "report",
        "content_b64": "eA==", "name": "a.md",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("route", STORY_ROUTES)
async def test_a_story_route_refuses_no_token_an_expired_one_and_another_storys(route):
    async with local_http() as h:
        goal, story = await h.seed_story()
        _, other = await h.seed_story(goal_id=goal)
        body = _body(story, goal)
        assert (await h.post(route, body)).status_code == 401, "no token"
        assert (await h.post(route, body, token=h.expired(story, goal))).status_code == 401, "expired"
        r = await h.post(route, body, token=h.story_token(other, goal))
        assert r.status_code == 403, f"another story's token: {r.status_code} {r.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/get-state", "/recent-messages", "/done-check", "/artifact-list"])
async def test_a_story_route_serves_its_own_storys_token(route):
    async with local_http() as h:
        goal, story = await h.seed_story()
        r = await h.post(route, _body(story, goal), token=h.story_token(story, goal))
        assert r.status_code == 200, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("route", HUMAN_ROUTES)
async def test_a_human_route_refuses_the_agent_strangers_and_the_wrong_goal(route):
    async with local_http() as h:
        goal, story = await h.seed_story()
        other_goal = await h.seed_goal()
        body = _body(story, goal)
        r = await h.post(route, body, token=h.story_token(story, goal))
        assert r.status_code == 403, f"agent token decided: {r.status_code} {r.text}"
        r = await h.post(route, body, token=h.human("stranger"))
        assert r.status_code == 403, f"non-member decided: {r.status_code} {r.text}"
        r = await h.post(route, {**body, "goal_id": other_goal}, token=h.human("person"))
        assert r.status_code in (403, 404), f"wrong goal: {r.status_code} {r.text}"
        assert (await h.post(route, body)).status_code in (401, 403), "no token"


@pytest.mark.asyncio
async def test_story_token_minting_is_for_the_assignee_only():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await h.ensure_user("stranger")
        r = await h.post("/story-token", {"story_id": story}, token=h.human("stranger"))
        assert r.status_code == 403
        assert r.json().get("reason") == "not_assignee"
        r = await h.post("/story-token", {"story_id": "no-such-story"}, token=h.human("person"))
        assert r.status_code == 404


@pytest.fixture
def no_firebase(monkeypatch):
    """A token that is not an extension session falls through to the browser
    (Firebase) login check, which answers 401 for a token it cannot verify. Stubbed
    so the result does not depend on whatever Firebase state an earlier test left."""
    from fastapi import HTTPException
    import app.routers.local_common as lc

    async def _not_a_browser_login(_request):
        raise HTTPException(status_code=401, detail="not a browser login")
    monkeypatch.setattr(lc, "get_current_user", _not_a_browser_login)


@pytest.mark.asyncio
async def test_the_story_list_is_the_callers_own(no_firebase):
    async with local_http() as h:
        goal, mine = await h.seed_story(state="accepted", held_by=None)
        _, theirs = await h.seed_story(goal_id=goal, state="accepted", held_by=None, assigned_to="someone")
        r = await h.get("/stories", token=h.human("person"))
        assert r.status_code == 200, r.text
        ids = {s["id"] for s in r.json()["stories"]}
        assert mine in ids and theirs not in ids
        assert (await h.get("/stories", token=h.story_token(mine, goal))).status_code in (401, 403)
