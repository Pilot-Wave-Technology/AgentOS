"""Run controls act on THIS thread's story and nothing else.

THE CONSTRAINT THIS PINS, in the words it was asked for: "the thread from where
something is getting paused should only work for the current active story on
that thread and nothing else."

So every entry point takes a thread id and derives the story from
`threads.story_id`. None of them accepts a story id, and that is not a
convenience — a control that took one could reach any run in the goal from any
conversation, including one somebody else is working. Stage K gave every story
its own thread so that this binding exists; these tests are what stop it being
quietly widened later.

The other half is the STATE. Pause only means something to a run with a leg;
resume only to one holding on a pause. The service reads what the run is in and
offers only what machine v5 already allows, rather than keeping a second opinion
about which transitions exist.

**How to run.** Needs the local stack's PostgreSQL with the kernel schema;
SKIPS without it. One transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.services.kernel_run_control import (
    PAUSABLE,
    RESUMABLE,
    TERMINAL,
    ControlRefused,
    _live_run,
    _thread_story,
)
from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

asyncpg = pytest.importorskip("asyncpg")

G = "zz-runctl-goal"
OTHER = "zz-runctl-other"


@contextlib.asynccontextmanager
async def _fixture():
    conn = await _connect_or_skip()
    if not await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='kernel' AND table_name='executions'"):
        await conn.close()
        pytest.skip("no kernel schema here")
    tx = conn.transaction()
    await tx.start()
    try:
        for gid in (G, OTHER):
            await conn.execute(
                "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                gid, "run control fixture", "zz-runctl-user")
        yield conn
    finally:
        await tx.rollback()
        left = await conn.fetchval(
            "SELECT count(*) FROM goals WHERE id LIKE 'zz-runctl%'")
        await conn.close()
        assert left == 0, "fixture survived the rollback — dev DB dirtied"


async def _story(conn, sid, goal=G):
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, created_by) "
        "VALUES ($1,$2,$3,'zz-runctl-user')", sid, goal, f"title {sid}")


async def _thread(conn, tid, sid, goal=G):
    await conn.execute(
        "INSERT INTO threads (id, goal_id, agent_id, story_id, created_by, "
        " title) VALUES ($1,$2,'zz-a',$3,'zz-runctl-user',$4)",
        tid, goal, sid, f"t {sid}")


async def _run(conn, sid, state, goal=G) -> int:
    return await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, state, goal_id, story_id)"
        " VALUES ('story','coding',$1,$2,$3) RETURNING id", state, goal, sid)


def test_the_thread_names_the_story_and_only_that_story():
    """Two threads, two stories. Each resolves to its own."""
    async def run():
        async with _fixture() as conn:
            await _story(conn, "zz-s1")
            await _story(conn, "zz-s2")
            await _thread(conn, "zz-t1", "zz-s1")
            await _thread(conn, "zz-t2", "zz-s2")

            assert (await _thread_story(conn, "zz-t1", G))[0] == "zz-s1"
            assert (await _thread_story(conn, "zz-t2", G))[0] == "zz-s2"
    asyncio.run(run())


def test_a_thread_in_another_goal_is_refused():
    """A thread id is caller-supplied. Without the goal check a member of one
    goal could name a thread in another and drive its executor."""
    async def run():
        async with _fixture() as conn:
            await _story(conn, "zz-s9", goal=OTHER)
            await _thread(conn, "zz-t9", "zz-s9", goal=OTHER)
            with pytest.raises(ControlRefused):
                await _thread_story(conn, "zz-t9", G)
    asyncio.run(run())


def test_a_thread_about_no_story_controls_nothing():
    """The general assistant conversation has no executor to stop."""
    async def run():
        async with _fixture() as conn:
            await conn.execute(
                "INSERT INTO threads (id, goal_id, agent_id, created_by, title)"
                " VALUES ('zz-t0',$1,'zz-a','zz-runctl-user','general')", G)
            with pytest.raises(ControlRefused):
                await _thread_story(conn, "zz-t0", G)
    asyncio.run(run())


def test_only_the_live_run_is_controlled():
    """A story amended after finishing goes DONE -> PENDING and gets a SECOND
    execution. The controls belong to the one that is live, not the history."""
    async def run():
        async with _fixture() as conn:
            await _story(conn, "zz-s1")
            old = await _run(conn, "zz-s1", "DONE")
            new = await _run(conn, "zz-s1", "RUNNING")
            got = await _live_run(conn, "zz-s1")
            assert got["id"] == new, f"picked {got['id']}, wanted {new}"
            assert got["id"] != old
    asyncio.run(run())


def test_a_story_whose_runs_all_finished_has_nothing_to_control():
    async def run():
        async with _fixture() as conn:
            await _story(conn, "zz-s1")
            for state in TERMINAL:
                await _run(conn, "zz-s1", state)
            assert await _live_run(conn, "zz-s1") is None
    asyncio.run(run())


def test_the_state_sets_decide_what_is_offered():
    """Read off machine v5's edges rather than restated. Pause needs a leg to
    stop; resume needs a park to release; cancel works from everywhere."""
    assert PAUSABLE == ("RUNNING",)
    assert set(RESUMABLE) == {"PARKED_HOT", "PARKED_COLD"}
    assert set(TERMINAL) == {"DONE", "FAILED", "CANCELLED"}
    # Nothing pausable is also terminal, or a finished run would show a button.
    assert not set(PAUSABLE) & set(TERMINAL)
    assert not set(RESUMABLE) & set(TERMINAL)


def test_the_service_takes_no_story_id_from_a_caller():
    """The scoping guarantee, asserted against the signatures themselves.

    If someone later adds a `story_id` parameter for convenience, the control
    stops being thread-scoped and this is the only thing that would notice.
    """
    import inspect

    from app.services import kernel_run_control as rc

    for name in ("pause", "resume", "cancel", "controls_for"):
        params = list(inspect.signature(getattr(rc, name)).parameters)
        assert params[0] == "thread_id", f"{name} must lead with thread_id"
        assert "story_id" not in params, (
            f"{name} accepts a story_id — the thread must be the only thing "
            f"that decides which run is controlled")


def test_a_resume_message_is_delivered_as_a_steer():
    """A message typed on Resume is a person speaking TO the executor, so it
    travels as a `steer` — not as the answer to the direction question.

    That matters for ordering as much as for framing. `standing_steers` reloads
    every accepted steer at the start of every leg, and `take_proposals` drains
    the mailbox at the TOP of the step loop, before the request is built —
    "a steer that lands a step late has already paid for a step of work in the
    direction it was trying to change." So the executor reads it before its
    next model call, which is the whole point.

    It must be submitted BEFORE the un-park, or the resumed leg can start and
    drain an empty mailbox.
    """
    import inspect

    from app.services import kernel_run_control as rc

    src = inspect.getsource(rc.resume)
    assert '"steer"' in src, "the message must travel as a steer"
    assert '"instruction"' in src, "steer payload key is `instruction`"
    # Ordering: the steer submit has to come before the decide() that un-parks.
    assert src.index('"steer"') < src.index("decide("), (
        "the steer must be in the mailbox before the run is un-parked, or the "
        "leg drains before it lands")


def test_resume_never_sends_the_word_approved_as_the_message():
    """`decide` substitutes "approved" for an empty answer on an approve
    verdict. Right for a plan; wrong here — the executor would read
    `WHAT THEY SAID: approved` in a message about a pause nobody approved
    anything in. And when there IS a message, the un-park text must point at
    the steer rather than restate it, so the two things the executor reads do
    not say different things about the same words."""
    import inspect

    from app.services import kernel_run_control as rc

    src = inspect.getsource(rc.resume)
    assert "Carry on" in src, "an empty resume needs real words, not 'approved'"
    assert "standing steer" in src, (
        "the un-park text must point at the steer, or the two things the "
        "executor reads say different things about the same message")


def test_verifying_can_be_cancelled_but_not_paused():
    """Asked directly: what happens if you pause or cancel during verification?

    Pause means nothing there — the adjudicator runs deterministic checks, not
    a model loop, so there is no leg and no step boundary to stop at. Cancel
    does work: VERIFYING is in the mailbox's NO_LEG_STATES and machine v5 has
    a VERIFYING -> CANCELLED edge.
    """
    from kernel.worker.mailbox import NO_LEG_STATES

    assert "VERIFYING" not in PAUSABLE
    assert "VERIFYING" not in TERMINAL          # so can_cancel stays true
    assert "VERIFYING" in NO_LEG_STATES, (
        "cancel reaches a verifying run through the mailbox; if VERIFYING "
        "left that set, Cancel would silently do nothing there")

