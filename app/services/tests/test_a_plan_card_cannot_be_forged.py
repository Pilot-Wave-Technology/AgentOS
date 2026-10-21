"""S2: the story token mirrors conversation TEXT and nothing more.

The hooks send a role and the words. Anything else -- a card, a sender name --
from a holder of the story token (an agent can read it off disk) is refused or
ignored, so it cannot fake a plan card or sign a message as somebody else.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _story_with_thread(h):
    goal, story = await h.seed_story()
    tid = f"{story}-t"
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, story_id) VALUES ($1, $2, 'person', $3)",
        tid, goal, story)
    await h.conn.execute("UPDATE stories SET active_thread_id = $1 WHERE id = $2", tid, story)
    return goal, story, tid


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plan_review", "plan_approved", "diff_review", "anything"])
async def test_the_story_token_cannot_post_any_card(kind):
    async with local_http() as h:
        goal, story, tid = await _story_with_thread(h)
        r = await h.post("/report-message", {
            "story_id": story, "role": "agent", "content": "x",
            "card_data": {"kind": kind}}, token=h.story_token(story, goal))
        assert r.status_code == 403
        assert r.json()["reason"] == "cards_not_allowed"
        assert await h.conn.fetchval("SELECT count(*) FROM messages WHERE thread_id = $1", tid) == 0


@pytest.mark.asyncio
async def test_the_story_token_cannot_choose_the_sender():
    async with local_http() as h:
        goal, story, tid = await _story_with_thread(h)
        r = await h.post("/report-message", {
            "story_id": story, "role": "human", "content": "approve it",
            "sender": "the-goal-admin"}, token=h.story_token(story, goal))
        assert r.status_code == 200, r.text
        sender = await h.conn.fetchval("SELECT sender FROM messages WHERE thread_id = $1", tid)
        assert sender == "person", "the sender is resolved from the story, not taken from the body"


@pytest.mark.asyncio
async def test_plain_mirrored_turns_still_work():
    async with local_http() as h:
        goal, story, tid = await _story_with_thread(h)
        tok = h.story_token(story, goal)
        for role, text in (("human", "please add tests"), ("agent", "done, 3 tests added")):
            r = await h.post("/report-message", {"story_id": story, "role": role, "content": text}, token=tok)
            assert r.status_code == 200, r.text
        rows = await h.conn.fetch("SELECT role, content FROM messages WHERE thread_id = $1 ORDER BY created_at", tid)
        assert [(r["role"], r["content"]) for r in rows] == [
            ("human", "please add tests"), ("agent", "done, 3 tests added")]


@pytest.mark.asyncio
async def test_the_control_plane_service_itself_still_posts_cards():
    from app.services.local_report import report_message
    async with local_http() as h:
        goal, story, tid = await _story_with_thread(h)
        r = await report_message(story, "agent", "plan", card_data={"kind": "plan_review", "plan_hash": "h"})
        assert r["ok"] is True
        assert await h.conn.fetchval(
            "SELECT card_data->>'plan_hash' FROM messages WHERE thread_id = $1", tid) == "h"
