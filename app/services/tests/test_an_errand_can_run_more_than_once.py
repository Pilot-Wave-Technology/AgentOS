"""An errand's grant is not spent by one run.

WHAT THIS IS ABOUT. `propose_errand` pinned `max_runs = 1` when
recurrence was removed, conflating two different rules: nothing fires an
errand on a CLOCK (still true, still enforced), and an errand may only
ever run ONCE (never intended, and wrong).

RUN 345 IS THE DEMONSTRATION. Its pod was still being scheduled when the
run was cancelled at eighty seconds. `advance` had already marked the
schedule `paused = true, retired_reason = 'ran once, as proposed'` when
the execution was created -- so the errand had spent its only run on work
that never started, and there was no way to run it again. GO's answer to
"try dispatching it again" was `E_NO_RUN`.

ONE AT A TIME REPLACES THE CEILING. Two runs of one errand would share a
story, a workspace and a branch, so the second would write underneath the
first. `due` will not claim a schedule whose story has a live run -- and
a request made during one is not an error and is not dropped, it simply
waits.

Real Postgres, one transaction, always rolled back.
"""
from __future__ import annotations

import pytest

asyncpg = pytest.importorskip("asyncpg")

from kernel.assistant.context import Refused  # noqa: E402

from app.services.tests.test_errand_proposal_sql import (  # noqa: E402
    _connect_or_skip, _goal, _propose, _run)


# ── the grant survives its own first run ─────────────────────────────

def test_a_grant_is_not_retired_by_running():
    """The precondition for everything else here."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.worker.schedules import advance

        g = await _goal(conn)
        out = await approve(conn, g["goal"], await _propose(conn, g["goal"]),
                            "alice")
        await advance(conn, out["schedule"], 4242)

        row = await conn.fetchrow(
            "SELECT paused, retired_reason, max_runs, run_count "
            "  FROM kernel.errand_schedules WHERE id = $1", out["schedule"])
        assert row["paused"] is False
        assert row["retired_reason"] is None
        assert row["max_runs"] is None, (
            "a ceiling of 1 retires the grant on the next advance, which "
            "is run 345 again one run later")
        assert row["run_count"] == 1, "history is still counted"
    _run(body)


def test_a_second_run_can_be_asked_for():
    """And asking is just setting the time -- the sweep is still the one
    thing that creates an execution."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.assistant.errand_dispatch import dispatch_errand
        from kernel.worker.schedules import advance

        g = await _goal(conn)
        out = await approve(conn, g["goal"], await _propose(conn, g["goal"]),
                            "alice")
        await advance(conn, out["schedule"], 4242)

        got = await dispatch_errand(conn, g["goal"], out["story"], "alice")
        assert not isinstance(got, Refused), got
        due_at = await conn.fetchval(
            "SELECT next_run_at FROM kernel.errand_schedules WHERE id = $1",
            out["schedule"])
        assert due_at is not None, "nothing will ever pick this up"
    _run(body)


def test_a_revoked_grant_cannot_be_re_run():
    """Re-running is running, so it needs the same live grant. A revoked
    errand answering "queued" would be the whole authorisation model
    leaking out through the retry path."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.assistant.errand_dispatch import dispatch_errand

        g = await _goal(conn)
        out = await approve(conn, g["goal"], await _propose(conn, g["goal"]),
                            "alice")
        await conn.execute(
            "UPDATE kernel.errand_schedules SET revoked_at = now() "
            " WHERE id = $1", out["schedule"])
        got = await dispatch_errand(conn, g["goal"], out["story"], "alice")
        assert isinstance(got, Refused) and got.code == "E_NO_GRANT", got
    _run(body)


def test_an_expired_grant_cannot_be_re_run():
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.assistant.errand_dispatch import dispatch_errand

        g = await _goal(conn)
        out = await approve(conn, g["goal"], await _propose(conn, g["goal"]),
                            "alice")
        await conn.execute(
            "UPDATE kernel.errand_schedules "
            "   SET expires_at = now() - interval '1 hour' WHERE id = $1",
            out["schedule"])
        got = await dispatch_errand(conn, g["goal"], out["story"], "alice")
        assert isinstance(got, Refused) and got.code == "E_NO_GRANT", got
    _run(body)


# ── one at a time ────────────────────────────────────────────────────

def test_a_request_made_during_a_live_run_waits_rather_than_starting():
    """NOT AN ERROR AND NOT LOST. `due` skips the schedule while a run of
    its story is non-terminal; the request stays in `next_run_at` and is
    claimed once the live run is terminal. A person who asks twice gets
    two runs, one after the other, and no refusal in between."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.assistant.errand_dispatch import dispatch_errand
        from kernel.worker.schedules import due

        g = await _goal(conn)
        out = await approve(conn, g["goal"], await _propose(conn, g["goal"]),
                            "alice")
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (story_id, goal_id, state, "
            "        generation, kind, domain, machine_version) "
            "VALUES ($1, $2, 'RUNNING', 0, 'story', 'ops', "
            "        (SELECT max(version) FROM kernel.run_state_machine)) "
            "RETURNING id", out["story"], g["goal"])
        assert eid

        # REFUSED, NOT QUEUED. Queueing behind the live run would be
        # safe -- `due` skips it -- but silent: somebody would press it,
        # see nothing, and press again. Say a run is already going and
        # let them ask when it matters.
        got = await dispatch_errand(conn, g["goal"], out["story"], "alice")
        assert isinstance(got, Refused) and got.code == "E_ALREADY_RUNNING"
        assert str(eid) in got.message, "it does not say WHICH run"

        # and the belt-and-braces half: even if a time were set, the
        # sweep would not claim a second run over the same branch.
        await conn.execute(
            "UPDATE kernel.errand_schedules SET next_run_at = now() "
            " WHERE id = $1", out["schedule"])
        assert [r for r in await due(conn) if r["id"] == out["schedule"]] == [], (
            "a second run was claimed while one was already live -- two "
            "runs of one errand share a story, a workspace and a branch")
    _run(body)


def test_it_is_claimed_once_the_live_run_is_terminal():
    """The other half: the guard delays, it does not block."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.assistant.errand_dispatch import dispatch_errand
        from kernel.worker.schedules import due

        g = await _goal(conn)
        out = await approve(conn, g["goal"], await _propose(conn, g["goal"]),
                            "alice")
        await conn.execute(
            "INSERT INTO kernel.executions (story_id, goal_id, state, "
            "        generation, kind, domain, machine_version) "
            "VALUES ($1, $2, 'CANCELLED', 0, 'story', 'ops', "
            "        (SELECT max(version) FROM kernel.run_state_machine))",
            out["story"], g["goal"])

        got = await dispatch_errand(conn, g["goal"], out["story"], "alice")
        assert not isinstance(got, Refused), got
        claimed = [r for r in await due(conn) if r["id"] == out["schedule"]]
        assert claimed, (
            "a cancelled run still blocks the errand, so run 345 could "
            "never be retried")
    _run(body)


# ── only the first run is automatic ──────────────────────────────────

def test_it_refuses_without_a_person():
    """A run spends the approver's budget and carries their name. The
    FIRST run is automatic because approving the errand was that
    decision; every one after it is somebody asking.

    Run 345 is why this is not theoretical: GO cancelled a healthy run
    on its own judgment, passing the goal owner's uid because it knew
    it. An agent that may also START runs that way spends money nobody
    asked it to."""
    async def body(conn):
        from kernel.assistant.errand_dispatch import dispatch_errand

        g = await _goal(conn)
        out = await approve_helper(conn, g)
        got = await dispatch_errand(conn, g["goal"], out["story"], "")
        assert isinstance(got, Refused) and got.code == "E_NO_HUMAN"
        assert "FIRST run is automatic" in got.message
    _run(body)


def test_it_refuses_an_ordinary_story():
    """Read off `work_kind` on the row, never from the caller -- the same
    rule `accept_errand` follows. An ordinary story is started by the
    person it belongs to."""
    async def body(conn):
        from kernel.assistant.errand_dispatch import dispatch_errand

        g = await _goal(conn)
        out = await approve_helper(conn, g)
        await conn.execute(
            "UPDATE stories SET work_kind = 'story' WHERE id = $1",
            out["story"])
        got = await dispatch_errand(conn, g["goal"], out["story"], "alice")
        assert isinstance(got, Refused) and got.code == "E_NOT_AN_ERRAND"
    _run(body)


def test_it_refuses_a_story_from_another_goal():
    async def body(conn):
        from kernel.assistant.errand_dispatch import dispatch_errand

        g = await _goal(conn)
        got = await dispatch_errand(conn, g["goal"], "no-such-story", "alice")
        assert isinstance(got, Refused) and got.code == "E_NO_SUCH_STORY"
    _run(body)


async def approve_helper(conn, g):
    from app.services.errand_grants import approve
    return await approve(conn, g["goal"], await _propose(conn, g["goal"]),
                         "alice")
