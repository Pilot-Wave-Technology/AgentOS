"""A refused submit returns the story to the agent.

THE DEADLOCK. `propose_completion` moves the story to `submitting`, and
the boundary hook refuses source edits in that phase -- because a change
after the work is submitted is one the person merging never saw. Both
are right. Together, with a refusal in between, they are a trap: the
checks fail, the story stays `submitting`, the agent CANNOT edit the
file that would fix them, and the extension's poll re-submits every
sixty seconds for ever.

Seen on 9519fc57, 2026-10-14: `grep -q "POST /add" README.md` failed and
the story had no way forward at all.

`release_claim` was written for exactly this and called by nothing --
the fourth engine in this build to be correct, tested and unreachable.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.tests.local_http import local_http

CHECK = {"run": "cd repos/acme__api && grep -q 'POST /add' README.md",
         "label": "goal"}


async def _claimed(h, goal: str, story: str) -> None:
    """A story whose agent has claimed completion: plan approved,
    checks owed, phase `submitting`."""
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story, tid)
    from app.services.local_plan_gate import approve_local_plan, submit_local_plan
    await submit_local_plan(goal, story, ["do it"], "because", [CHECK])
    await approve_local_plan(goal, story, "person")
    await h.conn.execute(
        "UPDATE stories SET current_phase = 'submitting' WHERE id = $1", story)


async def _submit(h, story: str, sha: str):
    from app.services import local_diff
    with patch.object(local_diff, "get_pool", lambda: h.conn), \
         patch.object(local_diff, "_repo_ref", AsyncMock(return_value=None)), \
         patch.object(local_diff, "post_diff_review",
                      AsyncMock(return_value={"ok": True})):
        return await local_diff.submit_local_diff(story, "d", "1 file", False, sha)


@pytest.mark.asyncio
async def test_failing_checks_put_the_story_back_in_executing():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        r = await _submit(h, story, "sha1")
        assert r["reason"] == "checks_not_passed"
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story) == "executing", (
            "the story is stuck at `submitting`, where the boundary refuses "
            "the very edit that would fix the check")


@pytest.mark.asyncio
async def test_the_agent_is_told_what_failed():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        await _submit(h, story, "sha1")
        said = " ".join(r["content"] or "" for r in await h.conn.fetch(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story))
        assert "did not go through" in said
        assert "POST /add" in said, "the refusal does not name the failing check"
        assert "propose_completion" in said, "it is not told how to try again"


@pytest.mark.asyncio
async def test_no_new_approval_is_demanded():
    """Failing checks are not a failed story. The plan the person
    approved still describes the work — the same rule the integration
    cycle already follows."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        await _submit(h, story, "sha1")
        assert await h.conn.fetchval(
            "SELECT count(*) FROM plan_versions WHERE story_id = $1 "
            "  AND status = 'approved'", story) == 1


@pytest.mark.asyncio
async def test_reporting_a_pass_then_resubmitting_goes_through():
    """The way out has to actually work, or the release is cosmetic."""
    from app.services.local_checks import record, refusal

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        await _submit(h, story, "sha1")
        await record(h.conn, story, "sha1", [{**CHECK, "passed": True}])
        assert await refusal(h.conn, story, "sha1") is None


@pytest.mark.asyncio
async def test_a_story_that_never_claimed_is_not_moved():
    """`release_claim` only touches `submitting`. A refusal on a story
    somebody is still writing must not rewrite its phase."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _claimed(h, goal, story)
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'planning' WHERE id = $1", story)
        await _submit(h, story, "sha1")
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story) == "planning"


def test_every_refusal_path_hands_it_back():
    """Pinned: a gate added later that returns its refusal directly puts
    the deadlock back, and nothing would notice until somebody hit it."""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "local_diff.py").read_text(encoding="utf-8")
    fn = src[src.index("async def submit_local_diff("):]
    fn = fn[:fn.index("\nasync def ", 10)]
    # Every gate that refuses AFTER the completion claim must go through
    # `_hand_it_back`. The errand and no-plan refusals come before the
    # claim exists, so they are listed as the deliberate exceptions.
    direct = re.findall(r"return \{\s*\n?\s*\"ok\": False", fn)
    assert len(direct) <= 2, (
        f"{len(direct)} refusals return directly; a submit refused after "
        "the claim leaves the story at `submitting`, where the boundary "
        "refuses the edit that would fix it")
