"""Catching up is approved once, at the plan. Submitting it is not a gate.

ONE APPROVAL COVERS THE WHOLE CATCH-UP. The person approved a plan that
says "take trunk in, run these checks, commit" -- there is nothing left
for a second approval to decide, and asking for one would be asking the
same question twice about work that has not changed since the answer.

THIS IS THE SAME RULE AS EVERYWHERE ELSE IN THE LANE, which is the
point: after the 2026-10-14 revision, submitting IS approving. The
catch-up is not an exception to that, it is an instance of it.

WHAT STILL GATES IT: the checks. An integration that took trunk in and
broke the suite is precisely the case the catch-up exists to catch --
somebody changed a contract, renamed a function, moved a file -- so the
submit refuses until every check of the cycle has passed at the commit
being pushed, including the suite checks the integration plan ADDED.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _waiting_and_behind(h):
    """A local story whose work is pushed and whose trunk has moved."""
    goal, story = await h.seed_story(state="awaiting_merge")
    await h.conn.execute(
        "UPDATE stories SET execution_mode = 'local', "
        "       current_phase = 'awaiting_merge' WHERE id = $1", story)
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story, tid)

    node = await h.seed_node(story, goal, state="QUEUED", owner="acme",
                             name="api", trunk="main")
    repo = await h.conn.fetchval(
        "SELECT git_repo_id FROM git_nodes WHERE id = $1", node)
    await h.conn.execute(
        "INSERT INTO git_repo_trunks (git_repo_id, trunk_branch, trunk_sha, "
        "   trunk_sha_seen_at) VALUES ($1, 'main', 'bbbb', "
        "   now() + interval '1 hour') "
        "ON CONFLICT (git_repo_id, trunk_branch) DO UPDATE "
        "   SET trunk_sha = EXCLUDED.trunk_sha", repo)
    await h.conn.execute(
        "UPDATE git_nodes SET base_sha = 'aaaa' WHERE id = $1", node)
    return goal, story


async def _offer_and_approve(h, goal: str, story: str) -> None:
    from app.services.local_integration import sweep
    from app.services.local_plan_gate import approve_local_plan

    assert await sweep(h.conn) == 1
    assert (await approve_local_plan(goal, story, "person"))["ok"]


# ── the approval that exists ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_approving_the_plan_authorises_the_submit():
    """The work plan was spent by the first submit, so without the
    integration approval there is nothing authorising another push.
    With it, there is -- and it is the only approval in the loop."""
    async with local_http() as h:
        goal, story = await _waiting_and_behind(h)
        from app.services.local_cycles import spend_cycle
        from app.services.local_plan_gate import approve_local_plan, submit_local_plan
        await submit_local_plan(goal, story, ["do it"], "because",
                                [{"run": "cd repos/acme__api && pytest",
                                  "label": "goal"}])
        await approve_local_plan(goal, story, "person")
        # THE SUBMIT, AS IT REALLY HAPPENS: it spends the cycle AND puts
        # the story back to `awaiting_merge`. Approving a work plan now
        # moves a waiting story to `in_progress` -- correctly, since it
        # is being worked again -- and a story in progress is not
        # offered a catch-up at all. Leaving the fixture at
        # `in_progress` made the sweep return nothing and the test fail
        # for the one reason that is not about catch-ups.
        await spend_cycle(h.conn, story, "submit")
        await h.conn.execute(
            "UPDATE stories SET state = 'awaiting_merge', "
            "       current_phase = 'awaiting_merge' WHERE id = $1", story)

        await _offer_and_approve(h, goal, story)
        assert await h.conn.fetchval(
            "SELECT count(*) FROM plan_versions WHERE story_id = $1 "
            "  AND status = 'approved'", story) == 1, (
            "the catch-up did not re-authorise a submit, so the story is "
            "stuck: its work plan is spent and nothing else is approved")


@pytest.mark.asyncio
async def test_the_catch_up_joins_the_cycle_rather_than_opening_one():
    from app.services.local_cycles import current_cycle

    async with local_http() as h:
        goal, story = await _waiting_and_behind(h)
        await _offer_and_approve(h, goal, story)
        assert await current_cycle(h.conn, story) == 1
        assert await h.conn.fetchval(
            "SELECT cycle FROM plan_versions WHERE story_id = $1 "
            "  AND kind = 'integration'", story) == 1


@pytest.mark.asyncio
async def test_the_story_is_integrating_while_it_happens():
    """Not `executing`: the phase is what tells the session, the panel
    and the next person which of the two is going on, and a source edit
    mid-merge is the one way to lose work that was already agreed."""
    async with local_http() as h:
        goal, story = await _waiting_and_behind(h)
        await _offer_and_approve(h, goal, story)
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1",
            story) == "integrating"


# ── and the one that does not ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_submit_lands_it_back_at_awaiting_merge_with_no_card():
    """No diff card, because there is nothing left to approve: the PR
    the story is already waiting on simply gets the new commits."""
    from app.services.local_diff import _park_for_review

    async with local_http() as h:
        goal, story = await _waiting_and_behind(h)
        await _offer_and_approve(h, goal, story)
        await _park_for_review(h.conn, story, {"ok": True})
        row = await h.conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", story)
        assert row["state"] == "awaiting_merge"
        assert row["current_phase"] == "awaiting_merge"
        assert await h.conn.fetchval(
            "SELECT count(*) FROM approvals WHERE story_id = $1 "
            "  AND kind = 'diff' AND decision IS NULL", story) == 0


@pytest.mark.asyncio
async def test_the_submit_spends_the_catch_up():
    """Otherwise the approval authorises every later push too, and the
    next person to ask for more work would be told the cycle is open."""
    from app.services.local_cycles import cycle_is_open
    from app.services.local_diff import _park_for_review

    async with local_http() as h:
        goal, story = await _waiting_and_behind(h)
        await _offer_and_approve(h, goal, story)
        await _park_for_review(h.conn, story, {"ok": True})
        assert await cycle_is_open(h.conn, story) is False


# ── what DOES still gate it ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_added_suite_checks_gate_the_integration_submit():
    """THE WHOLE POINT OF ASKING A PERSON. The integration plan adds
    each merged mount's suite, and taking trunk in is exactly when a
    renamed function or a moved file breaks one."""
    from app.services.local_checks import outstanding

    async with local_http() as h:
        goal, story = await _waiting_and_behind(h)
        await _offer_and_approve(h, goal, story)
        missing = await outstanding(h.conn, story, "newsha")
        assert missing, "the catch-up could be submitted untested"
        assert any("git grep" in m for m in missing), (
            "the conflict-marker guard is not among the required checks")


@pytest.mark.asyncio
async def test_reporting_them_clears_the_submit():
    from app.services.local_checks import record, refusal

    async with local_http() as h:
        goal, story = await _waiting_and_behind(h)
        await _offer_and_approve(h, goal, story)
        from app.services.local_checks import required
        await record(h.conn, story, "newsha",
                     [{**c, "passed": True} for c in
                      await required(h.conn, story)])
        assert await refusal(h.conn, story, "newsha") is None
