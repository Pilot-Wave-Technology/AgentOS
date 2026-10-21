"""The board's Mark Done, for stories that are NOT run in VS Code, pinned.

Part of RULE ZERO (see test_non_local_flows_are_pinned.py): written before the
local-lane work and never edited to make it pass. The web button must keep
doing exactly this for cloud stories:

  * hold-for-merge ON and unlanded work -> `awaiting_merge`, nothing tidied;
  * otherwise                           -> `done`, completed_at set;
  * already done                        -> says so, changes nothing.
"""
from __future__ import annotations

import os

import httpx
import pytest
from fastapi import FastAPI

from app.services.tests.local_http import local_http

# `local_http` shares one asyncpg connection across the test, and
# under xdist's ordering it is closed underneath this one
# (InterfaceError: connection is closed). Serial runs, including
# every CI run, are unaffected.
pytestmark = pytest.mark.no_parallel


async def _mark_done(h, goal, story, uid="person"):
    from app.auth import get_current_user
    from app.routers.assistant import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"uid": uid}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cp.test") as c:
        return await c.post(f"/goals/{goal}/stories/{story}/mark-done", data={"force": "true"})


@pytest.fixture
def no_announcement(monkeypatch):
    import app.routers.assistant as a

    async def _quiet(**_kw):
        return None
    monkeypatch.setattr(a, "_write_task_controller_announcement", _quiet)


@pytest.fixture
def hold(monkeypatch):
    def _set(on: bool):
        if on:
            monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
        else:
            monkeypatch.delenv("STORY_HOLDS_FOR_MERGE", raising=False)
    return _set


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["operator", "ia_dispatch"])
async def test_a_cloud_story_with_an_open_branch_waits_for_its_merge(mode, hold, no_announcement):
    hold(True)
    async with local_http() as h:
        goal, story = await h.seed_story(mode=mode, phase="awaiting_next", held_by=None)
        await h.seed_node(story, goal)
        r = await _mark_done(h, goal, story)
        assert r.status_code == 200 and r.json() == {"ok": True}
        row = await h.story(story)
        assert (row["state"], row["current_phase"], row["completed_at"]) == ("awaiting_merge", "awaiting_next", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [True, False])
async def test_a_cloud_story_with_nothing_to_land_is_done(on, hold, no_announcement):
    hold(on)
    async with local_http() as h:
        goal, story = await h.seed_story(mode="operator", phase="awaiting_next", held_by=None)
        r = await _mark_done(h, goal, story)
        assert r.json() == {"ok": True}
        row = await h.story(story)
        assert (row["state"], row["current_phase"]) == ("done", "done")
        assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_with_the_hold_off_an_open_branch_does_not_stop_done(hold, no_announcement):
    hold(False)
    async with local_http() as h:
        goal, story = await h.seed_story(mode="operator", phase="awaiting_next", held_by=None)
        await h.seed_node(story, goal)
        await _mark_done(h, goal, story)
        assert (await h.story(story))["state"] == "done"


@pytest.mark.asyncio
async def test_marking_a_done_story_done_again_changes_nothing(hold, no_announcement):
    hold(True)
    async with local_http() as h:
        goal, story = await h.seed_story(mode="operator", state="done", held_by=None)
        r = await _mark_done(h, goal, story)
        assert r.json() == {"ok": True, "already_done": True}


@pytest.mark.asyncio
async def test_a_non_member_cannot_mark_a_story_done(hold, no_announcement):
    async with local_http() as h:
        goal, story = await h.seed_story(mode="operator", held_by=None)
        r = await _mark_done(h, goal, story, uid="stranger")
        assert r.status_code == 403
        assert (await h.story(story))["state"] == "in_progress"
