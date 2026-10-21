"""S1 + S2: a local plan is approved by a PERSON, and binds to what was submitted.

`/plan-ingest` took the story-scoped agent token and recorded the plan as
approved by the assignee -- the agent could approve its own plan without a person
ever seeing it. It is gone. And `/plan-approve` bound the approval to the newest
`plan_review` card on the thread, which the agent token could post itself with any
hash; the story token can no longer post cards at all.
"""
from __future__ import annotations

import importlib

import pytest

from app.services.tests.local_http import local_http

PLAN = {"steps": ["change the thing"], "rationale": "because",
        "checks": [{"run": "cd repos/acme__api && npm test", "label": "guard"}]}


async def _thread(h, goal, story):
    tid = f"{story}-t"
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, story_id) VALUES ($1, $2, 'person', $3)",
        tid, goal, story)
    await h.conn.execute("UPDATE stories SET active_thread_id = $1 WHERE id = $2", tid, story)
    return tid


@pytest.mark.asyncio
async def test_plan_ingest_no_longer_exists():
    async with local_http() as h:
        goal, story = await h.seed_story()
        r = await h.post("/plan-ingest", {"story_id": story, "goal_id": goal, "plan_md": "x"},
                         token=h.story_token(story, goal))
        assert r.status_code in (404, 405)
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("app.services.local_ingest")


@pytest.mark.asyncio
async def test_the_story_token_can_submit_but_not_approve():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        tok = h.story_token(story, goal)
        r = await h.post("/plan-submit", {"story_id": story, "goal_id": goal, **PLAN}, token=tok)
        assert r.status_code == 200, r.text
        r = await h.post("/plan-approve", {"story_id": story, "goal_id": goal}, token=tok)
        assert r.status_code == 403
        assert (await h.story(story))["current_phase"] == "awaiting_plan_approval"
        assert await h.conn.fetchval("SELECT count(*) FROM plan_versions WHERE story_id = $1", story) == 0


@pytest.mark.asyncio
async def test_a_person_approves_and_the_approval_binds_to_the_submitted_hash():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        tok = h.story_token(story, goal)
        submitted = (await h.post("/plan-submit", {"story_id": story, "goal_id": goal, **PLAN}, token=tok)).json()
        forged = await h.post("/report-message", {
            "story_id": story, "role": "agent", "content": "x",
            "card_data": {"kind": "plan_review", "plan_hash": "evil"}}, token=tok)
        assert forged.status_code == 403
        r = await h.post("/plan-approve", {"story_id": story, "goal_id": goal}, token=h.human("person"))
        assert r.status_code == 200, r.text
        row = await h.conn.fetchrow(
            "SELECT status, approved_by, plan_hash FROM plan_versions WHERE story_id = $1", story)
        assert (row["status"], row["approved_by"]) == ("approved", "person")
        assert row["plan_hash"] == submitted["plan_hash"] != "evil"
        assert (await h.story(story))["current_phase"] == "executing"


@pytest.mark.asyncio
async def test_someone_else_cannot_approve():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await h.post("/plan-submit", {"story_id": story, "goal_id": goal, **PLAN}, token=h.story_token(story, goal))
        r = await h.post("/plan-approve", {"story_id": story, "goal_id": goal}, token=h.human("stranger"))
        assert r.status_code == 403
        other_goal, _ = await h.seed_story()
        r = await h.post("/plan-approve", {"story_id": story, "goal_id": other_goal}, token=h.human("person"))
        assert r.status_code == 404, "a story is approved only through its own goal"
