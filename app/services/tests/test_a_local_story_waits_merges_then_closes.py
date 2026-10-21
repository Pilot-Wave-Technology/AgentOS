"""Mark Done waits for the merge, in the local lane as in the cloud one.

ABHISHEK'S RULE, 2026-10-13: the pull request is merged by a human
clicking merge on GitHub, and Mark Done is available only once every
lane has merged. With the diff-review gate gone, this is the only thing
standing between "submitted" and "finished" — so if it does not hold,
`done` can be true of a story whose branch is still open and whose PR
can still be closed.

THE QUESTION IS THE CLOUD'S OWN. `has_unlanded_work` is imported rather
than re-derived, because it already carries two answers that are easy
to get wrong: a story with NO nodes has nothing to land and must finish
normally, and an ERRAND has a branch that never lands — `reconcile`
cuts a workstream for one like any other story — so a node test alone
would park every errand waiting for a merge that cannot happen.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _with_thread(h, goal_id: str, story_id: str) -> None:
    """`report_message` writes to the story's active thread; without one
    there is nowhere for the instruction to land."""
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal_id)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story_id, tid)


async def _with_plan(h, story_id: str, status: str = "executed") -> None:
    """A story that has been planned AND delivered.

    `executed`, not `approved`, and the difference is the point: the
    submit SPENDS the cycle, so a story at the merge gate has an
    executed plan. An approved one means work was agreed and never
    submitted, which Mark Done now refuses -- see
    `test_an_approved_unspent_plan_blocks_mark_done`.
    """
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, plan_doc_path, "
        "   plan_json, proposed_by, status, approved_at, approved_by) "
        "VALUES (gen_random_uuid(), $1, 1, 'p.md', '{}'::jsonb, 'local', "
        "        $2, now(), 'person')", story_id, status)


# ── the gate ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unmerged_lane_blocks_mark_done():
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        await h.seed_node(story, goal, state="BRANCH_CREATED")
        why = await story_done_blocker(story)
        assert why and "has not landed" in why


@pytest.mark.asyncio
async def test_the_refusal_names_the_branch_so_it_is_actionable():
    """"Not yet" tells a person nothing. Which branch, and whether there
    is a PR to go and merge, is the whole of what they need."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        await h.seed_node(story, goal, state="MERGING", pr=42)
        why = await story_done_blocker(story)
        assert "PR #42" in why


@pytest.mark.asyncio
async def test_a_lane_with_no_pr_yet_says_so_rather_than_inventing_one():
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        await h.seed_node(story, goal, state="BRANCH_CREATED", pr=None)
        assert "no PR yet" in await story_done_blocker(story)


# ── and what must still be allowed through ───────────────────────────

@pytest.mark.asyncio
async def test_a_merged_lane_lets_the_story_finish():
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        await h.seed_node(story, goal, state="MERGED", pr=7)
        assert await story_done_blocker(story) is None


@pytest.mark.asyncio
async def test_an_abandoned_lane_is_not_waited_on():
    """A lane nobody is going to land must not hold the story open for
    ever — abandoning it IS the decision."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        await h.seed_node(story, goal, state="ABANDONED")
        assert await story_done_blocker(story) is None


@pytest.mark.asyncio
async def test_a_story_with_no_branch_at_all_finishes_normally():
    """A docs story, or one whose work was abandoned. It has nothing to
    land, and a blanket "wait for a merge" would strand it."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        _, story = await h.seed_story(state="in_progress")
        await _with_plan(h, story)
        assert await story_done_blocker(story) is None


@pytest.mark.asyncio
async def test_an_errand_is_never_made_to_wait():
    """THE TRAP. `reconcile` cuts a branch for an errand like any other
    story and only declines to create the MERGE story, because its work
    does not land. A node test alone answers "yes, unlanded" for every
    errand, and marking one done would park it waiting for a merge the
    platform guarantees will never happen."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", work_kind="errand")
        await _with_plan(h, story)
        await h.seed_node(story, goal, state="BRANCH_CREATED")
        assert await story_done_blocker(story) is None


@pytest.mark.asyncio
async def test_an_errand_needs_no_plan_either():
    """ABHISHEK, 2026-10-14: "for an errand Mark done can be available all
    the time". The merge gate was not the one catching it -- the PLAN gate
    was. Nothing writes a plan for an errand and the plan card is never
    offered for one, so requiring an approved plan parks every errand for
    ever on a condition it has no way to meet."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        _, story = await h.seed_story(state="in_progress", work_kind="errand")
        assert await story_done_blocker(story) is None, (
            "an errand was blocked from Mark Done")


@pytest.mark.asyncio
async def test_an_errand_is_free_of_both_gates_at_once():
    """The two gates are separate returns, and an errand that fails both
    would pass a test that only removed one of them."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", work_kind="errand")
        await h.seed_node(story, goal, state="MERGING", pr=9)   # unlanded
        assert await story_done_blocker(story) is None          # and no plan


@pytest.mark.asyncio
async def test_a_real_story_is_still_gated_so_the_exemption_is_narrow():
    """The exemption reads `work_kind`, and a default-NULL story must not
    fall through it."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        _, story = await h.seed_story(state="in_progress")
        await h.conn.execute(
            "UPDATE stories SET work_kind = NULL WHERE id = $1", story)
        assert await story_done_blocker(story) is not None


@pytest.mark.asyncio
async def test_the_plan_gate_still_comes_first():
    """Two conditions, and the plan one is the older. A story with no
    approved plan should hear about the plan, not about a merge."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await h.seed_node(story, goal, state="BRANCH_CREATED")
        why = await story_done_blocker(story)
        assert why and "no approved plan" in why


# ── multi-lane: every lane, not just one ─────────────────────────────

@pytest.mark.asyncio
async def test_one_merged_lane_is_not_enough_when_another_is_open():
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        await h.seed_node(story, goal, state="MERGED", name="api", pr=1)
        await h.seed_node(story, goal, state="MERGING", name="web", pr=2)
        why = await story_done_blocker(story)
        assert why and "PR #2" in why
        assert "PR #1" not in why, "a merged lane should not be listed as waiting"


# ── and the other half of the rule: asking for more ──────────────────

@pytest.mark.asyncio
async def test_more_work_on_an_errand_does_not_demand_a_plan():
    """ABHISHEK, 2026-10-14: Mark Done is always live on an errand, "but
    the user can choose to do more work". That choice must not route
    through the plan gate — there is no plan to spend and none will be
    registered, so sending the errand to `planning` would park it on the
    condition Mark Done was just released from."""
    from app.services.local_plan_gate import request_more_work

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", work_kind="errand")
        r = await request_more_work(goal, story, "also check the staging logs",
                                    "person")
        assert r["ok"], r
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1",
            story) != "planning", "an errand was sent back to planning"


@pytest.mark.asyncio
async def test_the_errand_is_still_told_what_else_to_do():
    from app.services.local_plan_gate import request_more_work

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", work_kind="errand")
        await _with_thread(h, goal, story)
        await request_more_work(goal, story, "also check the staging logs", "person")
        rows = await h.conn.fetch(
            "SELECT role, content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1)", story)
        assert len(rows) == 1, rows
        assert rows[0]["role"] == "human", "nothing would wake the session"
        assert "staging logs" in rows[0]["content"]
        assert "register_plan" not in rows[0]["content"], (
            "an errand was told to register a plan it will never write")


@pytest.mark.asyncio
async def test_a_real_story_is_still_sent_back_to_planning():
    """The exemption is narrow: more work on a coding story is a new cycle
    and still needs a new plan."""
    from app.services.local_plan_gate import request_more_work

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress")
        await _with_plan(h, story)
        await request_more_work(goal, story, "one more thing", "person")
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story) == "planning"


# ── and what the session is told it is working on ────────────────────

def test_an_errand_gets_an_errand_hint_not_a_planning_one():
    """A phase-less errand fell through to the `planning` hint: "Plan, do
    not build… a boundary hook refuses source edits until they approve".
    Untrue for an errand, and a reason to stop."""
    from app.services.local_dispatch import _phase_hint

    hint = _phase_hint(None, "errand")
    assert "errand" in hint.lower()
    assert "register_plan" not in hint
    assert "pull request" in hint, "it should say why there is nothing to merge"


def test_a_story_hint_is_unchanged_by_the_errand_branch():
    from app.services.local_dispatch import _phase_hint, _PHASE_HINT

    assert _phase_hint("executing", "story") == _PHASE_HINT["executing"]
    assert _phase_hint("executing") == _PHASE_HINT["executing"]


# ── and a cycle that was agreed and never delivered ──────────────────

@pytest.mark.asyncio
async def test_an_approved_unspent_plan_blocks_mark_done():
    """THE HOLE THE NODES CANNOT SEE. On 9519fc57 cycle 2 was approved,
    worked, and its submit refused by the checks — so the work sat
    committed in the clone, unpushed, while the git node still said
    MERGED from cycle 1. `has_unlanded_work` reads NODES, an unpushed
    cycle never touches one, and Mark Done sailed through. The work
    would have gone with the checkout."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await h.conn.execute(
            "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
            "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
            "   approved_by) "
            "VALUES (gen_random_uuid(), $1, 1, 1, 'work', 'p1.md', '{}'::jsonb, "
            "        'local', 'executed', now(), 'person')", story)
        await h.seed_node(story, goal, state="MERGED", pr=1)
        assert await story_done_blocker(story) is None, "the fixture is wrong"

        await h.conn.execute(
            "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
            "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
            "   approved_by) "
            "VALUES (gen_random_uuid(), $1, 2, 2, 'work', 'p2.md', '{}'::jsonb, "
            "        'local', 'approved', now(), 'person')", story)
        why = await story_done_blocker(story)
        assert why and "not been submitted" in why


@pytest.mark.asyncio
async def test_a_delivered_cycle_does_not_block_it():
    """`spend_cycle` marks the plan `executed` at the submit, which is
    what makes this a question about delivery rather than about having
    ever had a plan."""
    from app.services.local_cycles import spend_cycle
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story, status="approved")
        await h.seed_node(story, goal, state="MERGED", pr=1)
        assert await story_done_blocker(story) is not None
        await spend_cycle(h.conn, story, "submit")
        assert await story_done_blocker(story) is None


@pytest.mark.asyncio
async def test_an_errand_is_still_exempt():
    """It has no cycles at all, so "an undelivered cycle" cannot be true
    of one — and reading it as true would park every errand again."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        _, story = await h.seed_story(state="in_progress", work_kind="errand")
        await _with_plan(h, story)
        assert await story_done_blocker(story) is None
