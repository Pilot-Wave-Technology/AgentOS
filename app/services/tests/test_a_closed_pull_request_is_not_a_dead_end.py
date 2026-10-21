"""Somebody closed the pull request without merging it.

DETECTION IS LANE-AGNOSTIC AND THE RESPONSE WAS NOT. The engine sets
the node `ESCALATED` with `last_error = "PR closed unmerged..."` for
any story; `story_landing.PR_CLOSED_SQL` then joins
`kernel.executions`, which a local story does not have. So the cloud
asked what the person meant and local was never told at all.

The silence was total: the node sat ESCALATED, the story sat at
`awaiting_merge`, `has_unlanded_work` saw a lane that had not landed
so Mark Done refused for ever, and the panel went on saying "merge it
on GitHub" about a pull request that was closed.

NOT BLOCKED -- IN PROGRESS (Abhishek, 2026-10-14). A closed pull
request is not a failure and nothing is stuck: somebody looked at the
work and decided it was not ready. The story has more to do, which is
`in_progress`, and the way to do it is to ask for more work.

AND THE SAME BRANCH IS USED. The work never merged, so the branch
still carries it and its base never moved -- which is why this is not
`reopen_for_next_cycle`, and why none of that function's `base_sha`
trouble applies here.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http

CLOSED = "PR closed unmerged (#12)"


async def _thread(h, goal: str, story: str) -> None:
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story, tid)


async def _spent_plan(h, story: str) -> None:
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
        "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
        "   approved_by) "
        "VALUES (gen_random_uuid(), $1, 1, 1, 'work', 'p.md', '{}'::jsonb, "
        "        'local', 'executed', now(), 'person')", story)


async def _closed_pr_story(h, **kw):
    goal, story = await h.seed_story(state="awaiting_merge")
    await h.conn.execute(
        "UPDATE stories SET execution_mode = 'local', "
        "       current_phase = 'awaiting_merge' WHERE id = $1", story)
    await _thread(h, goal, story)
    node = await h.seed_node(story, goal, state="ESCALATED", pr=12, **kw)
    await h.conn.execute(
        "UPDATE git_nodes SET last_error = $2 WHERE id = $1", node, CLOSED)
    await _spent_plan(h, story)
    return goal, story, node


async def _sweep(h) -> int:
    from app.services.local_pr_closed import sweep
    return await sweep(h.conn)


# -- the story is told, and carries on --------------------------------

@pytest.mark.asyncio
async def test_the_story_goes_back_into_progress_not_blocked():
    async with local_http() as h:
        _, story, _ = await _closed_pr_story(h)
        assert await _sweep(h) == 1
        row = await h.conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", story)
        assert row["state"] == "in_progress", (
            "a closed pull request is not a failure -- nothing is stuck, and "
            "`blocked` is a recovery surface for something needing none")
        assert row["current_phase"] == "planning"


@pytest.mark.asyncio
async def test_it_says_what_happened_and_what_to_do():
    async with local_http() as h:
        _, story, _ = await _closed_pr_story(h)
        await _sweep(h)
        said = await h.conn.fetchval(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1) "
            "  ORDER BY created_at DESC LIMIT 1", story)
        assert "closed without being merged" in said
        assert "Ask for more work" in said
        assert "same branch" in said


@pytest.mark.asyncio
async def test_the_lane_is_left_escalated_until_somebody_asks():
    """Reviving it now would have the engine open a fresh pull request
    within a minute of a person deliberately closing one."""
    async with local_http() as h:
        _, story, node = await _closed_pr_story(h)
        await _sweep(h)
        assert await h.conn.fetchval(
            "SELECT state FROM git_nodes WHERE id = $1", node) == "ESCALATED"


@pytest.mark.asyncio
async def test_sweeping_twice_tells_them_once():
    async with local_http() as h:
        await _closed_pr_story(h)
        assert await _sweep(h) == 1
        assert await _sweep(h) == 0


@pytest.mark.asyncio
async def test_a_multi_repo_story_still_landing_is_not_told():
    """Its second repo is still open; the story has not stopped."""
    async with local_http() as h:
        goal, story, _ = await _closed_pr_story(h, name="api")
        await h.seed_node(story, goal, state="MERGING", name="web", pr=13)
        assert await _sweep(h) == 0


@pytest.mark.asyncio
async def test_a_CLOUD_story_is_not_touched():
    """RULE ZERO: the cloud has its own sweep and its own card."""
    async with local_http() as h:
        _, story, _ = await _closed_pr_story(h)
        await h.conn.execute(
            "UPDATE stories SET execution_mode = 'operator' WHERE id = $1", story)
        assert await _sweep(h) == 0


# -- asking for more work revives the SAME lane -----------------------

@pytest.mark.asyncio
async def test_more_work_brings_the_same_branch_back():
    from app.services.local_more_work import request_more_work

    async with local_http() as h:
        goal, story, node = await _closed_pr_story(h)
        await _sweep(h)
        before = await h.conn.fetchval(
            "SELECT branch_name FROM git_nodes WHERE id = $1", node)

        r = await request_more_work(goal, story, "fix the review comments", "u1")
        assert r["ok"] is True and r["mode"] == "new", r
        row = await h.conn.fetchrow(
            "SELECT state, branch_name, pr_number, last_error, cycle "
            "  FROM git_nodes WHERE id = $1", node)
        assert row["state"] == "IN_PROGRESS", "the lane is still escalated"
        assert row["branch_name"] == before, "a new branch was cut"
        assert row["pr_number"] is None, (
            "`_ensure_pr_open` pins a PR only when the number is NULL, so a "
            "node still carrying the closed one never gets a new one")
        assert row["last_error"] is None
        assert row["cycle"] == 1, (
            "the cycle was bumped -- nothing merged, so there is no round to "
            "archive and no base to re-stamp")


@pytest.mark.asyncio
async def test_a_healthy_lane_is_not_disturbed_by_more_work():
    from app.services.local_more_work import request_more_work

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _thread(h, goal, story)
        await _spent_plan(h, story)
        node = await h.seed_node(story, goal, state="MERGING", pr=20)
        await request_more_work(goal, story, "more", "u1")
        row = await h.conn.fetchrow(
            "SELECT state, pr_number FROM git_nodes WHERE id = $1", node)
        assert row["state"] == "MERGING" and row["pr_number"] == 20


# -- and nothing claims the pull request is still open ----------------

@pytest.mark.asyncio
async def test_mark_done_says_the_pr_was_closed():
    """Telling somebody to merge the pull request first sends them to
    GitHub to look for a button that is not there."""
    from app.services.local_dispatch import story_done_blocker

    async with local_http() as h:
        _, story, _ = await _closed_pr_story(h)
        why = await story_done_blocker(story)
        assert why and "closed without being merged" in why
        assert "merge the pull request first" not in why


@pytest.mark.asyncio
async def test_the_landing_block_marks_it_closed():
    async with local_http() as h:
        goal, story, _ = await _closed_pr_story(h)
        r = await h.post("/get-state", {"story_id": story},
                         token=h.story_token(story, goal))
        [pr] = r.json()["landing"]["prs"]
        assert pr["pr_closed"] is True


@pytest.mark.asyncio
async def test_an_ordinary_open_pr_is_not_marked_closed():
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await h.seed_node(story, goal, state="MERGING", pr=7)
        r = await h.post("/get-state", {"story_id": story},
                         token=h.story_token(story, goal))
        assert r.json()["landing"]["prs"][0]["pr_closed"] is False
