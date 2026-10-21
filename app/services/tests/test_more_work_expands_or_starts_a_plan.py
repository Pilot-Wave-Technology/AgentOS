"""One test per row of the more-work decision table (flow §3.10).

THE CONTROL PLANE DECIDES, NEVER THE AGENT. The question is whether the
current cycle is still open: open means this instruction belongs to the
plan already under discussion, closed means the last intent was
delivered and this is a new one.

WHY EACH ROW IS WORTH ITS OWN TEST. The modes are not interchangeable
and the wrong one is quiet: `expand` where `new` was right lets work
land under a plan nobody re-approved, and `new` where `expand` was right
abandons an approved plan mid-flight and renumbers the plan file a
person is reading.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _thread(h, goal_id: str, story_id: str) -> None:
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal_id)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story_id, tid)


async def _plan(h, story_id: str, version: int, status: str,
                cycle: int | None = None) -> None:
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
        "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
        "   approved_by) "
        "VALUES (gen_random_uuid(), $1, $2, $3, 'work', $4, '{}'::jsonb, "
        "        'local', $5, now(), 'person')",
        story_id, version, cycle or version,
        f"agentos_plans/{story_id}/plan-{(cycle or version):02d}.md", status)


async def _ask(h, goal: str, story: str, what: str = "also add retries") -> dict:
    from app.services.local_more_work import request_more_work
    return await request_more_work(goal, story, what, "u1")


async def _phase(h, story_id: str) -> str:
    return await h.conn.fetchval(
        "SELECT current_phase FROM stories WHERE id = $1", story_id)


# ── cycle open → expand ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_plan_yet_expands_into_the_brief():
    """Nothing has been agreed, so there is nothing to supersede: the
    instruction is part of what the first plan must cover."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'discovery' WHERE id = $1", story)
        r = await _ask(h, goal, story)
        assert r["mode"] == "expand"
        assert await _phase(h, story) == "discovery", (
            "a story that has not been scoped was pushed into planning")


@pytest.mark.asyncio
async def test_a_plan_waiting_for_approval_expands():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_plan_approval' "
            " WHERE id = $1", story)
        assert (await _ask(h, goal, story))["mode"] == "expand"


@pytest.mark.asyncio
async def test_executing_expands_and_closes_the_gate_again():
    """The definition of done has changed, so the approved plan no longer
    describes it. Source edits must be refused until the revision is
    approved — the pod learned this the hard way on errand 282."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'executing' WHERE id = $1", story)
        r = await _ask(h, goal, story)
        assert r["mode"] == "expand"
        assert await _phase(h, story) == "planning"


@pytest.mark.asyncio
async def test_submitting_still_expands_because_nothing_has_landed():
    """THE EASY ONE TO GET WRONG. `submitting` looks finished and is not:
    the submit is the cycle-ending event, and it has not completed."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'submitting' WHERE id = $1", story)
        assert (await _ask(h, goal, story))["mode"] == "expand"


@pytest.mark.asyncio
async def test_expanding_keeps_the_same_plan_file():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        r = await _ask(h, goal, story)
        assert r["plan_path"].endswith("plan-01.md")


@pytest.mark.asyncio
async def test_expanding_tells_the_agent_to_stop_and_revise():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        await _ask(h, goal, story, "handle the 429 case")
        rows = await h.conn.fetch(
            "SELECT role, content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story)
        assert [r["role"] for r in rows] == ["human"], (
            "only a human-role message dispatches a local session")
        said = rows[0]["content"]
        assert "handle the 429 case" in said
        assert "register_plan" in said and "SAME cycle" in said


# ── cycle spent → new ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_waiting_for_merge_starts_a_new_plan():
    """The submit delivered the intent. Anything further is a new one."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_merge' WHERE id = $1",
            story)
        r = await _ask(h, goal, story)
        assert r["mode"] == "new"
        assert r["plan_path"].endswith("plan-02.md")


@pytest.mark.asyncio
async def test_a_new_cycle_takes_the_story_back_out_of_its_wait():
    """`done` must stop being reachable: there is unlanded work again."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        await _ask(h, goal, story)
        row = await h.conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", story)
        assert row["state"] == "in_progress"
        assert row["current_phase"] == "planning"


@pytest.mark.asyncio
async def test_the_new_cycle_names_the_file_it_will_write():
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        await _ask(h, goal, story)
        said = await h.conn.fetchval(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story)
        assert "plan-02.md" in said and "NEW plan" in said


# ── integrating → queued ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_request_during_integration_is_held_not_refused():
    """Refusing would make the answer depend on a background sweep's
    timing — the person has no way to see one is running."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'integrating' WHERE id = $1",
            story)
        r = await _ask(h, goal, story, "and bump the timeout")
        assert r["ok"] is True
        assert r["mode"] == "queued"
        assert await _phase(h, story) == "integrating", (
            "the hold was not a hold — the phase moved under the catch-up")


@pytest.mark.asyncio
async def test_a_queued_request_keeps_the_instruction():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'integrating' WHERE id = $1",
            story)
        await _ask(h, goal, story, "and bump the timeout")
        said = await h.conn.fetchval(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story)
        assert "bump the timeout" in said


# ── closed → refused ─────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["done", "reviewed", "cancelled", "rejected"])
@pytest.mark.asyncio
async def test_a_closed_story_is_refused(state):
    async with local_http() as h:
        goal, story = await h.seed_story()
        await h.conn.execute(
            "UPDATE stories SET state = $2 WHERE id = $1", story, state)
        r = await _ask(h, goal, story)
        assert r["ok"] is False
        assert r["reason"] == "story_closed"
        assert state in r["error"], "the refusal should say which closed state"


@pytest.mark.asyncio
async def test_a_story_in_another_goal_is_not_found():
    """The goal is part of the lookup, not decoration: without it this is
    a cross-tenant write keyed on a guessable id."""
    async with local_http() as h:
        _, story = await h.seed_story()
        other, _ = await h.seed_story()
        r = await _ask(h, other, story)
        assert r["ok"] is False and r["reason"] == "story_not_found"


# ── errands ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_errand_expands_with_no_plan_anywhere_in_it():
    async with local_http() as h:
        goal, story = await h.seed_story(work_kind="errand")
        await _thread(h, goal, story)
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'executing' WHERE id = $1", story)
        r = await _ask(h, goal, story, "check the staging logs too")
        assert r["mode"] == "expand"
        assert r["plan_path"] == ""
        assert await _phase(h, story) == "executing", (
            "an errand was sent to planning for a plan it will never write")


@pytest.mark.asyncio
async def test_the_errand_instruction_mentions_no_plan():
    async with local_http() as h:
        goal, story = await h.seed_story(work_kind="errand")
        await _thread(h, goal, story)
        await _ask(h, goal, story, "check the staging logs too")
        said = await h.conn.fetchval(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story)
        assert "staging logs" in said
        assert "register_plan" not in said


# ── stacking ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_several_requests_stack_on_the_same_open_cycle():
    """"Several requests in a row stack in order on the same open cycle"
    (§3.10). Each must land as its own instruction — collapsing them
    would lose whichever one the agent did not act on first."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        first = await _ask(h, goal, story, "add retries")
        second = await _ask(h, goal, story, "and a timeout")
        assert first["mode"] == second["mode"] == "expand"
        assert first["plan_path"] == second["plan_path"], (
            "the second request renumbered the plan file")
        said = " ".join(r["content"] for r in await h.conn.fetch(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story))
        assert "add retries" in said and "and a timeout" in said


@pytest.mark.asyncio
async def test_the_mode_can_change_between_stacked_requests():
    """Asking twice either side of a submit is two different questions,
    and answering the second from the first's mode would put new intent
    under a delivered plan."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        assert (await _ask(h, goal, story))["mode"] == "expand"
        from app.services.local_cycles import spend_cycle
        await spend_cycle(h.conn, story, "submit")
        assert (await _ask(h, goal, story))["mode"] == "new"


# ── a parked CATCH-UP holds, it is not "revised" ─────────────────────

async def _catch_up_parked(h, story: str) -> None:
    """The platform's catch-up, waiting for a person."""
    await h.conn.execute(
        "UPDATE stories SET current_phase = 'awaiting_plan_approval', "
        "       pending_plan_md = '# Take trunk in' WHERE id = $1", story)
    from app.services.local_report import report_message
    await report_message(story, "agent", "Trunk moved.",
                         card_data={"kind": "plan_review",
                                    "plan_kind": "integration",
                                    "plan_path": "agentos_plans/x/plan-01-trunk.md",
                                    "steps": [], "checks": [],
                                    "plan_hash": "h"})


@pytest.mark.asyncio
async def test_a_parked_catch_up_holds_the_request():
    """THE FIRST REAL USE OF MORE-WORK DID SOMETHING USELESS. A parked
    catch-up sits at `awaiting_plan_approval` like any other plan, so
    the request took the EXPAND path and told the agent to "revise the
    plan you registered" — it did not register that plan, the PLATFORM
    wrote it, and there is nothing in it for an agent to revise. Worse,
    expanding moved the phase to `planning`, dropping the catch-up out
    of the state a person could approve it from."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        await _catch_up_parked(h, story)
        r = await _ask(h, goal, story, "also write a summary")
        assert r["mode"] == "queued", r
        assert await _phase(h, story) == "awaiting_plan_approval", (
            "the catch-up was dropped out of its approval state")


@pytest.mark.asyncio
async def test_an_ordinary_parked_plan_still_expands():
    """The hold is for the PLATFORM's plan. A plan the agent wrote is
    exactly the thing a person should be able to change by talking."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "approved")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_plan_approval', "
            "       pending_plan_md = '# Plan' WHERE id = $1", story)
        from app.services.local_report import report_message
        await report_message(story, "agent", "Plan submitted.",
                             card_data={"kind": "plan_review", "steps": [],
                                        "checks": [], "plan_hash": "h"})
        assert (await _ask(h, goal, story))["mode"] == "expand"


# ── and a held instruction is actually released ──────────────────────

@pytest.mark.asyncio
async def test_a_queued_instruction_is_released_when_the_hold_lifts():
    """THE OTHER HALF OF `queued`, WHICH DID NOT EXIST. `_queue` recorded
    the instruction and a comment claimed `drain_queued` released it —
    a function nobody had written. The person was told it was held and
    it was then silently never acted on."""
    from app.services.local_more_work import drain_queued

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'integrating' WHERE id = $1", story)
        assert (await _ask(h, goal, story, "and bump the timeout"))["mode"] == "queued"

        await h.conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_merge', "
            "       state = 'awaiting_merge' WHERE id = $1", story)
        assert await drain_queued(h.conn, goal, story) == 1
        said = " ".join(r["content"] or "" for r in await h.conn.fetch(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1) "
            "  AND role = 'human'", story))
        assert "bump the timeout" in said


@pytest.mark.asyncio
async def test_draining_twice_does_not_replay_the_same_instruction():
    """Two plans for one ask is worse than none, and the submit that
    drains is a retried path."""
    from app.services.local_more_work import drain_queued

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'integrating' WHERE id = $1", story)
        await _ask(h, goal, story, "one more thing")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_merge' WHERE id = $1", story)
        assert await drain_queued(h.conn, goal, story) == 1
        assert await drain_queued(h.conn, goal, story) == 0


def test_the_submit_is_what_drains_it():
    """Pinned: the hold lifts when the catch-up lands, and nothing else
    is watching for that moment."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "local_diff.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _park_for_review("):]
    fn = fn[:fn.index("\nasync def ", 10)]
    assert "drain_queued" in fn


# ── after a merge, the SAME branch goes back to work ─────────────────

async def _merged_lane(h, story: str, goal: str, **kw):
    node = await h.seed_node(story, goal, state="MERGED", pr=kw.pop("pr", 7), **kw)
    await h.conn.execute(
        "UPDATE git_nodes SET merge_commit_sha = 'abc123', cycle = 1 "
        " WHERE id = $1", node)
    return node


@pytest.mark.asyncio
async def test_a_fully_merged_story_takes_no_more_work():
    """SUPERSEDED, AND THE REPLACEMENT IS THE OPPOSITE CLAIM. These
    three tests pinned the reopen-after-merge path: the same branch
    came back to life, the landed round was archived, both lanes
    reopened together.

    Abhishek, 2026-10-14: "more work should always be done before
    merging -- that is how work happens everywhere." The merge is the
    commitment. So nothing reopens, and the lane is left exactly as it
    landed. `test_more_work_stops_at_the_merge.py` holds the refusal
    itself; this holds that nothing is touched on the way to it."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        node = await _merged_lane(h, story, goal, pr=7)

        r = await _ask(h, goal, story, "and add a health endpoint")
        assert r["ok"] is False and r["reason"] == "already_merged", r
        row = await h.conn.fetchrow(
            "SELECT state, cycle, pr_number FROM git_nodes WHERE id = $1", node)
        assert row["state"] == "MERGED", "the lane was brought back to life"
        assert row["cycle"] == 1, "the cycle was bumped on a refused request"
        assert row["pr_number"] == 7, "the landed PR was unpinned"
        assert await h.conn.fetchval(
            "SELECT count(*) FROM git_node_landings WHERE git_node_id = $1",
            node) == 0, "a round was archived for work that is simply finished"


@pytest.mark.asyncio
async def test_the_story_is_left_where_it_was():
    """A refusal must not half-move the story. It was waiting to be
    closed out before the request and it still is after."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await h.conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_closeout' "
            " WHERE id = $1", story)
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        await _merged_lane(h, story, goal)
        await _ask(h, goal, story, "more")
        row = await h.conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", story)
        assert row["state"] == "awaiting_merge"
        assert row["current_phase"] == "awaiting_closeout"


@pytest.mark.asyncio
async def test_a_multi_repo_story_waits_for_its_sibling():
    """A multi-repo story merges one repo at a time. Reopening the
    first while the second is still in flight would put the group back
    in the queue halfway through the round it is still finishing."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        merged = await _merged_lane(h, story, goal, owner="acme", name="api")
        await h.seed_node(story, goal, state="MERGING", owner="acme",
                          name="web", pr=8)
        await _ask(h, goal, story, "more please")
        assert await h.conn.fetchval(
            "SELECT state FROM git_nodes WHERE id = $1", merged) == "MERGED", (
            "the merged lane was reopened while its sibling was still in "
            "flight")


@pytest.mark.asyncio
async def test_a_story_whose_every_lane_merged_is_refused():
    """SUPERSEDED. This pinned that both lanes reopened together once
    both had merged. A story with every lane merged is now finished:
    there is nothing to reopen and nothing to ask for."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        a = await _merged_lane(h, story, goal, owner="acme", name="api", pr=1)
        b = await _merged_lane(h, story, goal, owner="acme", name="web", pr=2)
        r = await _ask(h, goal, story, "more please")
        assert r["ok"] is False and r["reason"] == "already_merged"
        states = [x["state"] for x in await h.conn.fetch(
            "SELECT state FROM git_nodes WHERE id = ANY($1::text[])", [a, b])]
        assert states == ["MERGED", "MERGED"], states


@pytest.mark.asyncio
async def test_asking_before_the_merge_reopens_nothing():
    """The node is live and its PR is open; the next push updates it.
    Reopening would archive a round that has not landed."""
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _plan(h, story, 1, "executed")
        node = await h.seed_node(story, goal, state="MERGING", pr=5)
        await _ask(h, goal, story, "more please")
        assert await h.conn.fetchval(
            "SELECT state FROM git_nodes WHERE id = $1", node) == "MERGING"
        assert await h.conn.fetchval(
            "SELECT count(*) FROM git_node_landings WHERE git_node_id = $1",
            node) == 0
