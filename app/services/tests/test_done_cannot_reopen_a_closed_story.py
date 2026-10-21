"""S5: the board's Mark Done cannot move a finished VS Code story back to `done`.

Nothing checked the starting state, so a `reviewed` or `cancelled` local story
could be reopened as `done` from the web. Local stories only: rule zero keeps
the cloud path exactly as it is (pinned in test_web_mark_done_is_pinned.py).
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http
from app.services.tests.test_web_mark_done_is_pinned import _mark_done, no_announcement  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["reviewed", "cancelled", "rejected"])
async def test_a_finished_local_story_stays_finished(state, no_announcement):  # noqa: F811
    async with local_http() as h:
        goal, story = await h.seed_story(state=state, held_by=None)
        r = await _mark_done(h, goal, story)
        assert r.status_code == 409
        assert state in r.json()["error"]
        assert (await h.story(story))["state"] == state


@pytest.mark.asyncio
async def test_a_done_local_story_still_says_so(no_announcement):  # noqa: F811
    async with local_http() as h:
        goal, story = await h.seed_story(state="done", held_by=None)
        r = await _mark_done(h, goal, story)
        assert r.json() == {"ok": True, "already_done": True}
