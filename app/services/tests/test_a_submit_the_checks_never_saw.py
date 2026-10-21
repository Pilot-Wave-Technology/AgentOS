"""A submit at a commit the checks never ran on, and what the agent is told.

SEEN ON c72e8263, 2026-10-16. The checks passed at 47e495e. Running them
rewrote tracked `__pycache__/*.pyc` files, the extension's submit committed
those as d712ead and pushed it, and the gate refused d712ead because no
check had run there. The refusal said only "these checks have not passed
at d712ead", which reads as "your checks fail". The agent could not tell
that the checks had passed one commit earlier, or why the commit changed.

Two control-plane halves of the fix are pinned here:

  * the refusal names the commit the checks DID pass at;
  * the extension can hand a `submitting` story back when it refuses a
    submit itself (uncommitted changes after the checks), so the story is
    not left where the boundary blocks the edits that would fix it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.tests.local_http import local_http

CHECK = {"run": "cd repos/acme__api && python -m pytest", "label": "guard"}


async def _claimed(h, goal: str, story: str) -> None:
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2, execution_mode = 'local' "
        " WHERE id = $1", story, tid)
    from app.services.local_plan_gate import approve_local_plan, submit_local_plan
    await submit_local_plan(goal, story, ["do it"], "because", [CHECK])
    await approve_local_plan(goal, story, "person")
    await h.conn.execute(
        "UPDATE stories SET current_phase = 'submitting' WHERE id = $1", story)


async def _thread_text(h, story: str) -> str:
    return " ".join(r["content"] or "" for r in await h.conn.fetch(
        "SELECT content FROM messages WHERE thread_id = "
        "  (SELECT active_thread_id FROM stories WHERE id = $1)", story))


@pytest.mark.asyncio
async def test_the_refusal_names_where_the_checks_did_pass():
    from app.services.local_checks import record, refusal

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        await record(h.conn, story, "47e495efa0bc", [{**CHECK, "passed": True}])
        r = await refusal(h.conn, story, "d712ead5b0d8")
        assert r and r["reason"] == "checks_not_passed"
        assert "47e495efa0bc" in r["error"], (
            "the refusal does not say the checks passed at an earlier commit, "
            "so it reads as 'your checks fail' when they did not")
        assert "run_check" in r["error"], "it does not say how to clear it"


@pytest.mark.asyncio
async def test_a_refusal_with_no_earlier_pass_does_not_invent_one():
    from app.services.local_checks import refusal

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        r = await refusal(h.conn, story, "d712ead5b0d8")
        assert r and "last passed" not in r["error"]


@pytest.mark.asyncio
async def test_a_failed_run_is_not_named_as_a_pass():
    from app.services.local_checks import record, refusal

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        await record(h.conn, story, "aaaaaaaaaaaa", [{**CHECK, "passed": False}])
        r = await refusal(h.conn, story, "d712ead5b0d8")
        assert "aaaaaaaaaaaa" not in r["error"]


@pytest.mark.asyncio
async def test_the_extension_can_hand_a_refused_submit_back():
    from app.services import local_claim

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        with patch.object(local_claim, "get_pool", lambda: h.conn):
            r = await local_claim.release_refused_submit(
                story, "these changes are not committed: app/x.py")
        assert r == {"ok": True, "released": True}
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story) == "executing"
        said = await _thread_text(h, story)
        assert "app/x.py" in said and "propose_completion" in said


@pytest.mark.asyncio
async def test_a_story_not_submitting_is_left_alone_and_silent():
    from app.services import local_claim

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'executing' WHERE id = $1", story)
        before = await _thread_text(h, story)
        with patch.object(local_claim, "get_pool", lambda: h.conn):
            r = await local_claim.release_refused_submit(story, "anything")
        assert r["released"] is False
        assert await _thread_text(h, story) == before, (
            "a 'back to work' message was posted for a story already at work")


@pytest.mark.asyncio
async def test_the_route_is_scoped_to_the_story_token():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        _other_goal, other = await h.seed_story()
        token = h.story_token(story, goal)
        resp = await h.post("/claim-release", {"story_id": other, "why": "x"}, token)
        assert resp.status_code in (401, 403), (
            "a story token released another story's claim")
        resp = await h.post("/claim-release",
                            {"story_id": story, "why": "uncommitted"}, token)
        assert resp.status_code == 200 and resp.json()["released"] is True
