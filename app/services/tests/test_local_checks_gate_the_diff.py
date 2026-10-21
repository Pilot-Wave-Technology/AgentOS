"""The checks gate the submit, and they gate it AT A COMMIT.

THE PLATFORM RUNS THE CHECKS. That is the cloud's rule and it holds here
unchanged: an agent that both does the work and reports whether the work
is good is marking its own homework, and the failure mode is not
dishonesty but optimism -- a suite it believes it ran. The extension
runs them and posts the results; the control plane reads them.

"THE CHECKS PASSED" IS A PROPERTY OF A COMMIT, NOT OF A STORY. A pass
recorded before the last three commits says nothing about what is being
pushed, so every result carries the head sha it was measured at and the
gate asks about the sha being submitted. Drop the sha and the gate
degrades to "did they ever pass", which is true of every story that was
once green.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.tests.local_http import local_http

CHECK = {"run": "cd repos/acme__api && pytest -q", "label": "goal"}
SUITE = {"run": "cd repos/acme__api && make test", "label": "guard"}


async def _thread(h, goal: str, story: str) -> None:
    """The plan CARD is where the approved checks live, and a card is a
    message -- without a thread there is nowhere for it to be written."""
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story, tid)


async def _planned(h, goal: str, story: str, checks=(CHECK,)) -> None:
    """A story with an approved plan carrying `checks`."""
    from app.services.local_plan_gate import approve_local_plan, submit_local_plan

    await _thread(h, goal, story)
    await submit_local_plan(goal, story, ["do it"], "because", list(checks))
    await approve_local_plan(goal, story, "person")


async def _report(h, story: str, sha: str, results):
    from app.services.local_checks import record
    return await record(h.conn, story, sha, results)


async def _submit(h, story: str, sha: str):
    from app.services import local_diff
    with patch.object(local_diff, "get_pool", lambda: h.conn), \
         patch.object(local_diff, "_repo_ref", AsyncMock(return_value=None)), \
         patch.object(local_diff, "post_diff_review",
                      AsyncMock(return_value={"ok": True})):
        return await local_diff.submit_local_diff(story, "diff", "1 file",
                                                  False, sha)


# ── what the cycle owes ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_required_checks_come_from_the_approved_plan():
    from app.services.local_checks import required

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story, (CHECK, SUITE))
        assert [c["run"] for c in await required(h.conn, story)] == [
            CHECK["run"], SUITE["run"]]


@pytest.mark.asyncio
async def test_a_story_with_no_plan_owes_nothing():
    """The plan gate is what refuses that story, and it says so better."""
    from app.services.local_checks import outstanding

    async with local_http() as h:
        _, story = await h.seed_story()
        assert await outstanding(h.conn, story, "sha1") == []


# ── recording ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_reported_pass_clears_the_check():
    from app.services.local_checks import outstanding

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        await _report(h, story, "sha1", [{**CHECK, "passed": True}])
        assert await outstanding(h.conn, story, "sha1") == []


@pytest.mark.asyncio
async def test_a_reported_failure_does_not():
    from app.services.local_checks import outstanding

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        await _report(h, story, "sha1", [{**CHECK, "passed": False}])
        assert await outstanding(h.conn, story, "sha1") == [CHECK["run"]]


@pytest.mark.asyncio
async def test_re_running_a_check_replaces_its_result():
    """A row per attempt would make "did it pass" a question about which
    row you read. The honest answer is always the latest."""
    from app.services.local_checks import outstanding

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        await _report(h, story, "sha1", [{**CHECK, "passed": False}])
        await _report(h, story, "sha1", [{**CHECK, "passed": True}])
        assert await outstanding(h.conn, story, "sha1") == []
        assert await h.conn.fetchval(
            "SELECT count(*) FROM local_check_results WHERE story_id = $1",
            story) == 1


@pytest.mark.asyncio
async def test_the_same_check_in_two_mounts_is_two_results():
    """A story writing two repositories runs its suite in each, and "the
    suite passed" with no lane on it cannot say which."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        await _report(h, story, "sha1", [
            {**CHECK, "mount": "repos/acme__api", "passed": True},
            {**CHECK, "mount": "repos/acme__web", "passed": True}])
        assert await h.conn.fetchval(
            "SELECT count(*) FROM local_check_results WHERE story_id = $1",
            story) == 2


# ── the sha is the point ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_pass_at_an_older_commit_does_not_clear_a_newer_one():
    """THE DEFECT THIS EXISTS TO STOP. Without the sha on the row, three
    more commits after a green run would submit as though they had been
    tested."""
    from app.services.local_checks import outstanding

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        await _report(h, story, "old", [{**CHECK, "passed": True}])
        assert await outstanding(h.conn, story, "new") == [CHECK["run"]]


# ── the gate ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submitting_is_refused_until_the_checks_pass():
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        r = await _submit(h, story, "sha1")
        assert r["ok"] is False
        assert r["reason"] == "checks_not_passed"


@pytest.mark.asyncio
async def test_the_refusal_names_the_checks_and_the_commit():
    """"Checks have not passed" leaves a person guessing which -- and
    the usual answer is a suite the catch-up added, which is exactly the
    one they cannot guess."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story, (CHECK, SUITE))
        await _report(h, story, "sha1", [{**CHECK, "passed": True}])
        r = await _submit(h, story, "sha1")
        assert SUITE["run"] in r["error"]
        assert CHECK["run"] not in r["error"], (
            "a check that passed is listed as outstanding")
        assert "sha1" in r["error"]


# The two "gets through" cases ask the gate directly rather than driving
# the whole submit. Past the gate, `submit_local_diff` takes an advisory
# lock through `pool.acquire()` as a context manager, which the harness
# connection is not -- so a full submit here would fail for a reason
# that has nothing to do with checks, and a test that cannot tell those
# two failures apart proves nothing about either.

@pytest.mark.asyncio
async def test_a_clear_commit_gets_past_the_gate():
    from app.services.local_checks import refusal

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        await _report(h, story, "sha1", [{**CHECK, "passed": True}])
        assert await refusal(h.conn, story, "sha1") is None


@pytest.mark.asyncio
async def test_a_plan_with_no_checks_is_not_blocked_by_this_gate():
    """It is a weaker plan, and the plan validator is where that
    argument belongs. This gate must not become a second one."""
    from app.services.local_checks import refusal
    from app.services.local_plan_gate import approve_local_plan, submit_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _thread(h, goal, story)
        await submit_local_plan(goal, story, ["do it"], "because", [])
        await approve_local_plan(goal, story, "person")
        assert await refusal(h.conn, story, "sha1") is None


@pytest.mark.asyncio
async def test_the_gate_only_looks_at_the_current_cycle():
    """Last cycle's green run is not this cycle's evidence: the plan,
    and therefore the contract, has changed."""
    from app.services.local_checks import outstanding
    from app.services.local_cycles import spend_cycle

    async with local_http() as h:
        goal, story = await h.seed_story()
        await _planned(h, goal, story)
        await _report(h, story, "sha1", [{**CHECK, "passed": True}])
        await spend_cycle(h.conn, story, "submit")
        await _planned(h, goal, story)          # cycle 2, same check
        assert await outstanding(h.conn, story, "sha1") == [CHECK["run"]]
