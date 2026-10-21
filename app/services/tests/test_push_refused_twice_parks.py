"""A push refused twice for the same reason stops being retried.

THE EXTENSION RE-SUBMITS ON A TIMER while a story sits at `submitting`.
For a flake that is exactly right. For a protected branch, a revoked
installation or a repository that moved it is a loop: the refusal is
identical every time, nobody is told it will never succeed, and the
thread fills with one identical card a minute. Exec 407 was the same
mechanism in the pod lane, and the memory of it is why this exists
before anybody hits it here.

TWICE, NOT ONCE. A flake retried once and succeeding should cost nothing
and say nothing.

AND "IDENTICAL" INCLUDES THE DETAIL. Two different causes are two
problems, and the second has not had its attempt yet.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _story(h):
    goal, story = await h.seed_story(state="in_progress")
    await h.conn.execute(
        "UPDATE stories SET execution_mode = 'local' WHERE id = $1", story)
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story, tid)
    return goal, story


def _refusal(detail="the push didn't land (403)"):
    return {"ok": False, "reason": "push_not_on_remote", "error": detail}


async def _state(h, story):
    return await h.conn.fetchrow(
        "SELECT state, current_phase FROM stories WHERE id = $1", story)


# ── counting ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_first_refusal_is_just_a_refusal():
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        r = await park_if_stuck(h.conn, story, _refusal())
        assert r.get("parked") is not True
        assert (await _state(h, story))["state"] == "in_progress"


@pytest.mark.asyncio
async def test_the_second_identical_one_parks_the_story():
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        await park_if_stuck(h.conn, story, _refusal())
        r = await park_if_stuck(h.conn, story, _refusal())
        assert r["parked"] is True
        row = await _state(h, story)
        assert row["state"] == "blocked"
        assert row["current_phase"] == "blocked", (
            "the phase still says `submitting`, so the extension keeps "
            "retrying the thing we just gave up on")


@pytest.mark.asyncio
async def test_a_different_cause_starts_its_own_count():
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        await park_if_stuck(h.conn, story, _refusal("403 protected"))
        r = await park_if_stuck(h.conn, story, _refusal("404 repo moved"))
        assert r.get("parked") is not True
        assert (await _state(h, story))["state"] == "in_progress"


@pytest.mark.asyncio
async def test_a_successful_submit_ends_the_streak():
    """The count is "in a row". Without clearing it, one flake early in
    a story leaves the counter at 1 and an unrelated refusal weeks later
    parks on its first occurrence."""
    from app.services.local_push_park import clear, park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        await park_if_stuck(h.conn, story, _refusal())
        await clear(h.conn, story)
        r = await park_if_stuck(h.conn, story, _refusal())
        assert r.get("parked") is not True


@pytest.mark.asyncio
async def test_the_submit_path_clears_it_on_success():
    from app.services.local_diff import _park_for_review
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        await park_if_stuck(h.conn, story, _refusal())
        await _park_for_review(h.conn, story, {"ok": True})
        assert await h.conn.fetchval(
            "SELECT count(*) FROM local_push_refusals WHERE story_id = $1",
            story) == 0


# ── what a person is told ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_refusal_says_it_has_stopped_trying():
    """"Re-run Submit Diff" is the wrong advice at this point, and
    leaving it there is how a person spends ten minutes re-running it."""
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        await park_if_stuck(h.conn, story, _refusal())
        r = await park_if_stuck(h.conn, story, _refusal())
        assert "stopped retrying" in r["error"]
        assert "branch protection" in r["error"]


@pytest.mark.asyncio
async def test_a_card_is_posted_so_the_panel_can_show_it():
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        await park_if_stuck(h.conn, story, _refusal())
        await park_if_stuck(h.conn, story, _refusal())
        said = await h.conn.fetchval(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1) "
            "  ORDER BY created_at DESC LIMIT 1", story)
        assert "stopped retrying" in said


# ── and what it must not do ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_finished_story_is_not_dragged_back_to_blocked():
    """A late refusal arriving after somebody closed the story out must
    not reopen it."""
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, story = await _story(h)
        await h.conn.execute(
            "UPDATE stories SET state = 'done' WHERE id = $1", story)
        await park_if_stuck(h.conn, story, _refusal())
        await park_if_stuck(h.conn, story, _refusal())
        assert (await _state(h, story))["state"] == "done"


@pytest.mark.asyncio
async def test_one_storys_streak_is_not_anothers():
    from app.services.local_push_park import park_if_stuck

    async with local_http() as h:
        _, mine = await _story(h)
        _, theirs = await _story(h)
        await park_if_stuck(h.conn, mine, _refusal())
        r = await park_if_stuck(h.conn, theirs, _refusal())
        assert r.get("parked") is not True
