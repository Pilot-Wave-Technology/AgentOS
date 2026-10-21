"""`propose_completion`, the signal the local lane never had.

NOTHING MOVED A LOCAL STORY OUT OF `executing`. `_park_for_review` set
`awaiting_review` only after a successful submit, and the extension only
submitted once the story was ALREADY `awaiting_review` — a circle with no
entry point. The handler that used to break it went with the task DAG.
So on 2026-10-14 story 66317ffc's agent finished, wrote "All done" in
chat, and the story sat in `executing`; the only way on was a human
typing into the session.

IT IS A CLAIM. The agent cannot push — the boundary refuses it and it
holds no token — so this moves the story to `submitting` and the
extension does the rest. The same division the cloud lane draws, where
`propose_completion` records a belief and the platform acts on it.

Driven over HTTP through the real router, because every authorization
rule of this lane lives there.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _thread(h, goal_id: str, story_id: str) -> None:
    """A story thread to post into. `seed_story` leaves `active_thread_id`
    null, and `report_message` has nowhere to write without one."""
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal_id)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story_id, tid)


async def _approved_plan(h, story_id: str) -> None:
    """The open cycle's plan, approved — the precondition for a claim."""
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, plan_doc_path, "
        "                           plan_json, proposed_by, status, approved_at, "
        "                           approved_by) "
        "VALUES (gen_random_uuid(), $1, 1, 'p.md', '{}'::jsonb, 'local', "
        "        'approved', now(), 'person')", story_id)


# ── the claim ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_finished_agent_moves_the_story_to_submitting():
    async with local_http() as h:
        goal, story = await h.seed_story(phase="executing")
        await _approved_plan(h, story)
        r = await h.post("/propose-completion",
                         {"story_id": story, "goal_id": goal,
                          "summary": "Added the history endpoint."},
                         token=h.story_token(story, goal))
        assert r.status_code == 200, r.text
        assert r.json()["phase"] == "submitting"
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story) == "submitting"


@pytest.mark.asyncio
async def test_the_claim_is_announced_so_the_person_sees_it_happen():
    """The push takes a while and nothing else says it started."""
    async with local_http() as h:
        goal, story = await h.seed_story(phase="executing")
        await _thread(h, goal, story)
        await _approved_plan(h, story)
        await h.post("/propose-completion", {"story_id": story, "goal_id": goal},
                     token=h.story_token(story, goal))
        # EVERY message, not the latest: `now()` is transaction-scoped in
        # Postgres and the whole test is one transaction, so both messages
        # carry the same `created_at` and ORDER BY cannot separate them.
        said = " ".join(r["content"] or "" for r in await h.conn.fetch(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story))
        assert said and "pull request" in said.lower()


# ── what it refuses, and why each refusal is actionable ──────────────

@pytest.mark.asyncio
async def test_a_claim_before_the_plan_is_approved_is_refused():
    """Claiming work nobody authorised would reach a BRANCH here, not a
    card — there is no diff review left to catch it."""
    async with local_http() as h:
        goal, story = await h.seed_story(phase="executing")
        r = await h.post("/propose-completion", {"story_id": story, "goal_id": goal},
                         token=h.story_token(story, goal))
        assert r.status_code == 409
        assert r.json()["reason"] == "no_approved_plan"


@pytest.mark.asyncio
async def test_a_claim_while_still_planning_is_refused_and_says_the_phase():
    """An agent told only "no" tries again identically."""
    async with local_http() as h:
        goal, story = await h.seed_story(phase="planning")
        await _approved_plan(h, story)
        r = await h.post("/propose-completion", {"story_id": story, "goal_id": goal},
                         token=h.story_token(story, goal))
        assert r.status_code == 409
        body = r.json()
        assert body["reason"] == "wrong_phase"
        assert body["phase"] == "planning"


@pytest.mark.asyncio
async def test_a_second_claim_is_refused_while_the_first_is_in_flight():
    """`submitting` is not `executing`, so the guard above covers it —
    pinned separately because a double-claim is what a retrying agent
    does, and two pushes for one story is the thing to avoid."""
    async with local_http() as h:
        goal, story = await h.seed_story(phase="executing")
        await _approved_plan(h, story)
        tok = h.story_token(story, goal)
        first = await h.post("/propose-completion",
                             {"story_id": story, "goal_id": goal}, token=tok)
        assert first.status_code == 200
        second = await h.post("/propose-completion",
                              {"story_id": story, "goal_id": goal}, token=tok)
        assert second.status_code == 409
        assert second.json()["phase"] == "submitting"


@pytest.mark.asyncio
async def test_a_cloud_story_cannot_claim_through_the_local_route():
    async with local_http() as h:
        goal, story = await h.seed_story(phase="executing", mode="operator")
        await _approved_plan(h, story)
        r = await h.post("/propose-completion", {"story_id": story, "goal_id": goal},
                         token=h.story_token(story, goal))
        assert r.status_code == 400
        assert r.json()["reason"] == "not_local"


@pytest.mark.asyncio
async def test_a_token_for_another_story_cannot_claim_this_one():
    """The router's scope guard, which is why this runs over HTTP."""
    async with local_http() as h:
        goal, story = await h.seed_story(phase="executing")
        _, other = await h.seed_story(goal_id=goal, phase="executing")
        await _approved_plan(h, story)
        r = await h.post("/propose-completion", {"story_id": story, "goal_id": goal},
                         token=h.story_token(other, goal))
        assert r.status_code in (401, 403)


# ── the way back, when the submit cannot be made ─────────────────────

@pytest.mark.asyncio
async def test_a_failed_submit_returns_the_story_to_executing():
    """FAILING CHECKS ARE NOT A FAILED STORY. The agent carries on under
    the same approved plan — no new approval — and a story left in
    `submitting` would be stranded exactly as `executing` stranded it."""
    from app.services.local_claim import release_claim

    async with local_http() as h:
        goal, story = await h.seed_story(phase="executing")
        await _thread(h, goal, story)
        await _approved_plan(h, story)
        await h.post("/propose-completion", {"story_id": story, "goal_id": goal},
                     token=h.story_token(story, goal))
        await release_claim(story, "2 checks failed")
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story) == "executing"
        # EVERY message, not the latest: `now()` is transaction-scoped in
        # Postgres and the whole test is one transaction, so both messages
        # carry the same `created_at` and ORDER BY cannot separate them.
        said = " ".join(r["content"] or "" for r in await h.conn.fetch(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story))
        assert "2 checks failed" in said


# ── a catch-up ends the same way a work cycle does ──────────────────
#
# ONE ENDING FOR BOTH KINDS. `integrating` used to mean "the extension
# is taking the trunk in, the agent waits" -- a second machine with its
# own merge, its own checks and its own push, none of which the agent
# could see, and all four of its repeat bugs lived there. The agent now
# works the catch-up as a plan and finishes it the way it finishes any
# plan: by claiming completion.

async def _integrating(h):
    goal, story = await h.seed_story(state="awaiting_merge")
    await h.conn.execute(
        "UPDATE stories SET execution_mode = 'local', "
        "       current_phase = 'integrating' WHERE id = $1", story)
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
        "   status, plan_doc_path) "
        "VALUES (gen_random_uuid(), $1, 1, 1, 'integration', "
        "        'approved', 'agentos_plans/x/plan-01-trunk.md')", story)
    return goal, story


@pytest.mark.asyncio
async def test_a_finished_catch_up_can_claim_completion():
    from app.services.local_claim import propose_local_completion

    async with local_http() as h:
        goal, story = await _integrating(h)
        out = await propose_local_completion(goal, story, "Trunk is in.")
        assert out["ok"], out
        assert (await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story)
        ) == "submitting"


@pytest.mark.asyncio
async def test_a_failed_catch_up_submit_returns_to_INTEGRATING():
    """NOT to `executing`. The open plan is a trunk merge; dropping the
    story into `executing` would tell the agent it is doing ordinary
    feature work and hand it the `executing` hint, which says nothing
    about `sync_trunk`."""
    from app.services.local_claim import propose_local_completion, release_claim

    async with local_http() as h:
        goal, story = await _integrating(h)
        await propose_local_completion(goal, story, "Trunk is in.")
        await release_claim(story, "a check failed", pool=h.conn)
        assert (await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story)
        ) == "integrating"
