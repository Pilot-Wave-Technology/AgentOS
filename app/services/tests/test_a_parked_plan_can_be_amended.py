"""A plan waiting for approval can be changed by asking for a change.

REJECTING TO MOVE A SENTENCE IS TOO HEAVY. `reject_local_plan` sends the
story back to `planning` and throws the plan away — right when the
person wants a different approach, wrong when they want the same
approach with one more check. Abhishek, 2026-10-14: amending should be
possible "just by talking".

THE MECHANISM WAS ALREADY THERE AND THE PROMPT FORBADE IT.
`submit_local_plan` overwrites `pending_plan_md` and posts a fresh
card, and `_default_plan_path` reuses the number until a plan is
approved — ten rewordings leave one `plan-NN.md`. What stopped it was
the phase hint, which told the agent in so many words: "do not register
another plan over it".
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _parked_plan(h, goal_id: str, story_id: str) -> None:
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal_id)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story_id, tid)


PLAN = {"steps": ["write it"], "rationale": "because",
        "checks": [{"run": "cd repos/a__b && pytest", "label": "goal"}]}


@pytest.mark.asyncio
async def test_a_second_plan_replaces_the_one_on_the_card():
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        await submit_local_plan(goal, story, PLAN["steps"], PLAN["rationale"],
                                PLAN["checks"])
        await submit_local_plan(goal, story, ["write it", "and test it twice"],
                                "because, and they asked for more", PLAN["checks"])
        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "and test it twice" in md
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1",
            story) == "awaiting_plan_approval"


@pytest.mark.asyncio
async def test_amending_keeps_the_same_plan_file():
    """A cycle is one plan file. Bumping the number on every reword would
    leave a folder of near-identical plans and break the "more work needs
    plan-NN+1" rule, which counts cycles rather than edits."""
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        await submit_local_plan(goal, story, PLAN["steps"], PLAN["rationale"],
                                PLAN["checks"])
        first = await h.conn.fetchval(
            "SELECT pending_plan_path FROM stories WHERE id = $1", story)
        await submit_local_plan(goal, story, ["different"], "why",
                                PLAN["checks"])
        assert await h.conn.fetchval(
            "SELECT pending_plan_path FROM stories WHERE id = $1", story) == first


@pytest.mark.asyncio
async def test_amending_does_not_approve_anything():
    """Replacing the plan must not short-circuit the person. An agent that
    could re-register its way past approval is S1 by another door."""
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _parked_plan(h, goal, story)
        await submit_local_plan(goal, story, PLAN["steps"], PLAN["rationale"],
                                PLAN["checks"])
        await submit_local_plan(goal, story, ["again"], "why", PLAN["checks"])
        assert await h.conn.fetchval(
            "SELECT count(*) FROM plan_versions WHERE story_id = $1 "
            "  AND status = 'approved'", story) == 0


def test_the_hint_invites_an_amendment_instead_of_forbidding_it():
    """The code always allowed this; the prompt is what refused it."""
    from app.services.local_dispatch import _PHASE_HINT

    hint = _PHASE_HINT["awaiting_plan_approval"]
    assert "do not register another plan over it" not in hint
    assert "register_plan" in hint
    assert "Do not start" in hint, "amending is not a licence to start work"


def test_the_executing_hint_names_the_claim_not_a_human_submit():
    """It used to say "the person submits the diff when they are ready",
    which is no longer how a story finishes."""
    from app.services.local_dispatch import _PHASE_HINT

    hint = _PHASE_HINT["executing"]
    assert "propose_completion" in hint
    assert "the person submits the diff" not in hint


def test_the_retired_phases_are_gone_from_the_vocabulary():
    from app.services.local_dispatch import _LIFECYCLE_ACTION, _PHASE_HINT

    for dead in ("awaiting_review", "awaiting_next"):
        assert dead not in _PHASE_HINT
        assert dead not in _LIFECYCLE_ACTION
    for live in ("submitting", "awaiting_merge"):
        assert live in _PHASE_HINT
        assert live in _LIFECYCLE_ACTION
