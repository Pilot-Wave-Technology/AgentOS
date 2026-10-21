"""Approving the plan has to WAKE the session, not just record the approval.

WHAT HAPPENED ON 66317ffc (2026-10-14). Abhishek approved the plan in
the panel at 05:34:17. `approve_local_plan` wrote `current_phase =
'executing'` and posted a notice — and nothing else happened. At
05:34:58 he typed "approve the plan" into the session himself, and the
work started immediately.

THE DIFFERENCE IS THE ROLE. The notice is `role="agent"`: a record, and
records dispatch nothing. A `human` message is what wakes a local
session. `request_more_work` already knew this and posts one; plan
approval did not, so the one transition that should say GO said
nothing at all.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http
from app.services.tests._source import source as src


async def _parked_plan(h, goal_id: str, story_id: str) -> None:
    """A story with a plan registered and waiting for a person."""
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal_id)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2, "
        "       current_phase = 'awaiting_plan_approval', "
        "       pending_plan_md = '# Plan', "
        "       pending_plan_path = 'agentos_plans/x/plan-01.md' "
        " WHERE id = $1", story_id, tid)


async def _messages(h, story_id: str):
    return await h.conn.fetch(
        "SELECT role, content, sender FROM messages WHERE thread_id = "
        "  (SELECT active_thread_id FROM stories WHERE id = $1)", story_id)


@pytest.mark.asyncio
async def test_approving_posts_something_the_session_acts_on():
    """A `human` message dispatches; an `agent` one does not. This is the
    whole of the 41-second gap."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        r = await approve_local_plan(goal, story, "person")
        assert r["ok"], r
        roles = [m["role"] for m in await _messages(h, story)]
        assert "human" in roles, (
            "approval posted no human-role message, so nothing woke the "
            "session — the defect seen on 66317ffc")


@pytest.mark.asyncio
async def test_the_instruction_names_the_tool_that_finishes_the_story():
    """The agent has no other way to say it is done, and an agent that
    does not know the verb stops at a chat message instead."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        await approve_local_plan(goal, story, "person")
        said = " ".join(m["content"] or "" for m in await _messages(h, story))
        assert "propose_completion" in said


@pytest.mark.asyncio
async def test_the_approval_is_still_recorded_as_a_card():
    """The wake-up is additional, not a replacement: the panel reads the
    `plan_approved` card and a person reads who approved it."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        await approve_local_plan(goal, story, "person")
        msgs = await _messages(h, story)
        assert any((m["role"] == "agent" and "Plan approved by" in (m["content"] or ""))
                   for m in msgs)


@pytest.mark.asyncio
async def test_the_phase_still_moves_to_executing():
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        await approve_local_plan(goal, story, "person")
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story) == "executing"


def test_the_waking_message_carries_the_approvers_uid():
    """`sender` is what makes it that person's instruction rather than an
    anonymous one — the same thing `request_more_work` does."""
    body = src("app/services/local_plan_gate.py")
    fn = body[body.index("async def approve_local_plan("):]
    fn = fn[:fn.index("async def reject_local_plan(")]
    human = fn.index('story_id, "human",')
    # 400 characters was enough when the message was one sentence; the
    # integration wording made it two branches. The claim is about the
    # call, so slice to the call's end rather than to a byte count.
    assert "sender=approved_by_uid" in fn[human:fn.index(")", fn.index(
        "sender=", human))]


# ── approving a new cycle takes the story out of its wait ────────────

async def _waiting_with_parked_plan(h, goal: str, story: str,
                                    integration: bool = False) -> None:
    """A story at `awaiting_merge` with a plan registered on it."""
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2, state = 'awaiting_merge', "
        "       current_phase = 'awaiting_plan_approval', "
        "       pending_plan_md = '# Plan', pending_plan_path = 'p.md' "
        " WHERE id = $1", story, tid)
    from app.services.local_report import report_message
    card = {"kind": "plan_review", "steps": [], "checks": [], "plan_hash": "h"}
    if integration:
        card["plan_kind"] = "integration"
    await report_message(story, "agent", "Plan submitted.", card_data=card)


@pytest.mark.asyncio
async def test_approving_a_work_plan_takes_it_out_of_awaiting_merge():
    """TALKING TO THE SESSION IS A DOOR TOO. "Ask for more work" moves
    the state; typing in the terminal does not -- and the agent,
    finding its cycle spent, correctly registers a new plan by itself
    rather than walking into the submit gate. On ec6d5d1f that left the
    board reading `awaiting_merge` while work was actively happening,
    which is the exact failure `request_more_work` exists to prevent."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _waiting_with_parked_plan(h, goal, story)
        assert (await approve_local_plan(goal, story, "person"))["ok"]
        row = await h.conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", story)
        assert row["state"] == "in_progress", (
            "the board still says the story is waiting for a merge while "
            "its next cycle is being worked")
        assert row["current_phase"] == "executing"


@pytest.mark.asyncio
async def test_approving_a_CATCH_UP_leaves_it_waiting():
    """A catch-up is the platform taking the trunk in while the story
    waits for its merge. It is still waiting."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _waiting_with_parked_plan(h, goal, story, integration=True)
        await approve_local_plan(goal, story, "person")
        row = await h.conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", story)
        assert row["state"] == "awaiting_merge"
        assert row["current_phase"] == "integrating"


@pytest.mark.asyncio
async def test_a_story_not_waiting_is_not_touched():
    """The move is off `awaiting_merge` specifically. A `blocked` story
    approving a plan must not be quietly un-blocked."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _waiting_with_parked_plan(h, goal, story)
        await h.conn.execute(
            "UPDATE stories SET state = 'blocked' WHERE id = $1", story)
        await approve_local_plan(goal, story, "person")
        assert await h.conn.fetchval(
            "SELECT state FROM stories WHERE id = $1", story) == "blocked"


@pytest.mark.asyncio
async def test_approving_a_work_plan_does_NOT_reopen_a_merged_lane():
    """SUPERSEDED, AND INVERTED. This pinned that approving a plan put
    a merged lane back to work -- the fix for "talking to the session
    is the other door". The door is shut at the other end now: a story
    whose work has merged takes no more work at all, so nothing should
    reach this with a merged lane, and if it does, reviving the lane
    would be reopening work already accepted onto trunk."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _waiting_with_parked_plan(h, goal, story)
        node = await h.seed_node(story, goal, state="MERGED", pr=33)
        await h.conn.execute(
            "UPDATE git_nodes SET merge_commit_sha = 'abc', cycle = 1 "
            " WHERE id = $1", node)

        await approve_local_plan(goal, story, "person")
        row = await h.conn.fetchrow(
            "SELECT state, cycle, pr_number FROM git_nodes WHERE id = $1", node)
        assert row["state"] == "MERGED"
        assert row["cycle"] == 1
        assert row["pr_number"] == 33


@pytest.mark.asyncio
async def test_approving_cycle_one_reopens_nothing():
    """Nothing is merged, so there is nothing to reopen. The ordinary
    case must cost a query and change nothing."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        node = await h.seed_node(story, goal, state="BRANCH_CREATED")
        await approve_local_plan(goal, story, "person")
        row = await h.conn.fetchrow(
            "SELECT state, cycle FROM git_nodes WHERE id = $1", node)
        assert row["state"] == "BRANCH_CREATED"
        assert row["cycle"] == 1


@pytest.mark.asyncio
async def test_approving_a_CATCH_UP_does_not_reopen_anything():
    """The catch-up runs ON the lane that is waiting to merge. Reopening
    it would bump the cycle underneath the merge it is preparing for."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _waiting_with_parked_plan(h, goal, story, integration=True)
        node = await h.seed_node(story, goal, state="MERGED", pr=9)
        await h.conn.execute(
            "UPDATE git_nodes SET merge_commit_sha = 'abc', cycle = 1 "
            " WHERE id = $1", node)
        await approve_local_plan(goal, story, "person")
        assert await h.conn.fetchval(
            "SELECT state FROM git_nodes WHERE id = $1", node) == "MERGED"
