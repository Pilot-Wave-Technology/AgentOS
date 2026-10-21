"""Ec5 — an errand that runs again, without a second scheduler.

"Only the Conductor dispatches" has survived every stage, and it survives
because exactly one thing turns an intention into a running container. A
scheduler that created containers would be a second one, and the first time
the two disagreed — about a paused goal, an admission bucket, a kill switch
— the schedule would win, silently, because nothing else would know it had.

So the sweep creates a PENDING execution and stops, on the tick that
already exists. Everything below is either the cron arithmetic or the
claiming, and the claiming is the part that has to be right: two replicas
sweeping the same instant must produce one run, and a crash must lose a run
rather than duplicate one.

The cron half needs no database. The claiming half needs a real one, and a
mocked pool would answer whatever the mock was told about `FOR UPDATE SKIP
LOCKED`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest

from kernel.worker.schedules import BadCron, next_after, parse

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-sched-test"
NOW = dt.datetime(2026, 9, 11, 14, 3, tzinfo=dt.timezone.utc)   # a Friday


# ── the cron arithmetic ──────────────────────────────────────────────────

@pytest.mark.parametrize("expression,expected", [
    ("*/5 * * * *", "2026-09-11T14:05:00+00:00"),
    ("* * * * *", "2026-09-11T14:04:00+00:00"),
    ("0 * * * *", "2026-09-11T15:00:00+00:00"),
    ("0 9 * * 1", "2026-09-14T09:00:00+00:00"),      # Monday 09:00
    ("30 6 * * 1-5", "2026-09-14T06:30:00+00:00"),   # next weekday
    ("0 0 1 * *", "2026-10-01T00:00:00+00:00"),      # first of the month
    ("15,45 * * * *", "2026-09-11T14:15:00+00:00"),
    ("0 0 29 2 *", "2028-02-29T00:00:00+00:00"),     # leap day
])
def test_the_next_time_is_the_one_cron_would_pick(expression, expected):
    assert next_after(expression, NOW).isoformat() == expected


def test_day_of_month_and_weekday_are_or_ed_the_way_cron_does_it():
    """`0 0 13 * 5` is the 13th AND every Friday, not Friday the 13th.
    It surprises everybody, and matching cron rather than being clever is
    what makes an expression copied from a crontab do the same thing
    here."""
    # 2026-09-11 is a Friday, so the next Friday is the 18th — but the
    # 13th comes first, and OR takes it.
    assert next_after("0 0 13 * 5", NOW).day == 13


def test_the_answer_is_strictly_after_the_moment_asked():
    """Otherwise a schedule that fires at 14:03 computes 14:03 as its next
    time, is immediately due again, and runs every four seconds."""
    at_the_minute = dt.datetime(2026, 9, 11, 14, 0, tzinfo=dt.timezone.utc)
    assert next_after("0 * * * *", at_the_minute) > at_the_minute


@pytest.mark.parametrize("bad,fragment", [
    ("* * * *", "five fields"),
    ("* * * * * *", "five fields"),
    ("", "five fields"),
    ("99 * * * *", "outside 0-59"),
    ("* 25 * * *", "outside 0-23"),
    ("x * * * *", "not a number"),
    ("*/0 * * * *", "positive"),
    ("5-1 * * * *", "outside"),
    ("0 0 31 2 *", "no next occurrence"),
])
def test_an_unusable_expression_says_what_is_wrong(bad, fragment):
    """A schedule is written by a person in a form; the error is what they
    read. "invalid cron" sends them to a manual."""
    with pytest.raises(BadCron) as caught:
        next_after(bad, NOW)
    assert fragment in str(caught.value)


def test_parse_returns_the_sets_it_says_it_does():
    minute, hour, dom, month, dow = parse("*/15 9-17 * * 1-5")
    assert minute == {0, 15, 30, 45}
    assert hour == set(range(9, 18))
    assert dom == set(range(1, 32))
    assert month == set(range(1, 13))
    assert dow == {1, 2, 3, 4, 5}


# ── the claiming ─────────────────────────────────────────────────────────

async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
        ok = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'kernel' AND table_name IN "
            "('errand_schedules','executions')")
        if ok == 2:
            return conn
        await conn.close()
    pytest.skip("no local database with the kernel schema — start the stack")


def _run(body):
    async def _go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(_go())


async def _schedule(conn, cron="*/5 * * * *", due_in_seconds=-60, **kw):
    when = (None if due_in_seconds is None
            else dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=due_in_seconds))
    return await conn.fetchval(
        """
        INSERT INTO kernel.errand_schedules
            (goal_id, kind, domain, task, name, cron, next_run_at, paused)
        VALUES ($1,'errand','ops',$2,$3,$4,$5,$6) RETURNING id
        """,
        kw.get("goal", f"{PREFIX}-{os.urandom(3).hex()}"),
        kw.get("task", "check the nightly report ran"),
        kw.get("name", "nightly check"), cron, when, kw.get("paused", False))


def test_the_database_refuses_a_schedule_with_no_task_or_no_cron():
    """SAVEPOINT PER ATTEMPT, AND THE NESTING ORDER MATTERS.

    A failed statement poisons the surrounding transaction in Postgres, so
    without a savepoint the SECOND insert fails with "current transaction
    is aborted" — a different error, and one that would make this test
    pass or fail for reasons unrelated to the constraint.

    `pytest.raises` OUTSIDE the transaction, not inside. Inside, it
    swallows the exception, the context manager sees success, and tries to
    RELEASE a savepoint the database has already aborted. The exception
    has to travel through the transaction so it rolls back, and only then
    be caught.
    """
    async def body(conn):
        for task, cron in (("  ", "* * * * *"), ("a task", "")):
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                async with conn.transaction():      # a savepoint
                    await conn.execute(
                        "INSERT INTO kernel.errand_schedules "
                        "(goal_id, task, cron) VALUES ($1,$2,$3)",
                        f"{PREFIX}-bad", task, cron)
    _run(body)


def test_a_due_schedule_is_claimed_exactly_once():
    """THE PART THAT HAS TO BE RIGHT. Selecting and then updating would
    let two replicas both see the same due row; the UPDATE is the claim,
    so the second finds nothing."""
    from kernel.worker.schedules import due

    async def body(conn):
        sid = await _schedule(conn)
        first = await due(conn)
        assert [r["id"] for r in first] == [sid]
        # A second sweep in the same instant — the other replica.
        assert await due(conn) == []
    _run(body)


def test_a_schedule_that_is_not_due_yet_is_left_alone():
    from kernel.worker.schedules import due

    async def body(conn):
        await _schedule(conn, due_in_seconds=3600)
        assert await due(conn) == []
    _run(body)


def test_a_paused_schedule_never_fires():
    """PAUSED IS A COLUMN, NOT A DELETION. "Stop this for now" and "this
    was a mistake" are different decisions, and a schedule deleted to
    pause is one nobody can restore or audit."""
    from kernel.worker.schedules import due

    async def body(conn):
        await _schedule(conn, paused=True)
        assert await due(conn) == []
    _run(body)


def test_a_retired_schedule_has_no_next_time_and_stays_quiet():
    from kernel.worker.schedules import due

    async def body(conn):
        await _schedule(conn, due_in_seconds=None)
        assert await due(conn) == []
    _run(body)


def test_the_sweep_creates_a_pending_execution_and_stops_there():
    """THE WHOLE DISCIPLINE. PENDING, not ADMITTED and not a container —
    the Conductor admits and dispatches it exactly as it would an errand
    somebody started by hand, so the kill switch still kills."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        goal = f"{PREFIX}-sweep-{os.urandom(3).hex()}"
        sid = await _schedule(conn, goal=goal, task="tail the error log")
        actions = await sweep(conn)
        assert [a["action"] for a in actions] == ["scheduled"]

        row = await conn.fetchrow(
            "SELECT id, state, kind, domain, task FROM kernel.executions "
            "WHERE goal_id = $1", goal)
        assert row["state"] == "PENDING", "the sweep dispatched something"
        assert row["kind"] == "errand" and row["domain"] == "ops"
        assert row["task"] == "tail the error log"

        after = await conn.fetchrow(
            "SELECT next_run_at, last_execution_id, run_count "
            "FROM kernel.errand_schedules WHERE id = $1", sid)
        assert after["next_run_at"] is not None, "it will never run again"
        assert after["last_execution_id"] == row["id"]
        assert after["run_count"] == 1
    _run(body)


def test_the_next_time_is_computed_forward_so_a_missed_window_runs_once():
    """A worker down for six hours with a five-minute schedule wakes to 72
    due firings. Running them is a thundering herd that answers a question
    nobody asked 71 times."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        sid = await _schedule(conn, cron="*/5 * * * *",
                              due_in_seconds=-6 * 3600)
        await sweep(conn)
        row = await conn.fetchrow(
            "SELECT next_run_at, run_count FROM kernel.errand_schedules "
            "WHERE id = $1", sid)
        assert row["run_count"] == 1, "it caught up by running repeatedly"
        assert row["next_run_at"] > dt.datetime.now(dt.timezone.utc)
        # And nothing is due any more.
        from kernel.worker.schedules import due
        assert await due(conn) == []
    _run(body)


def test_a_schedule_that_cannot_create_an_execution_is_still_advanced():
    """`due` sets `next_run_at` to NULL as its claim, so a schedule that
    is not advanced never runs again. A failure to create ONE execution
    silently retiring the schedule is much worse than the failure."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        # An unknown profile: `create_execution` resolves it and raises.
        sid = await conn.fetchval(
            "INSERT INTO kernel.errand_schedules "
            "(goal_id, kind, domain, task, cron, next_run_at) "
            "VALUES ($1,'nosuch','profile','t','*/5 * * * *', now()) "
            "RETURNING id", f"{PREFIX}-broken")
        actions = await sweep(conn)
        assert [a["action"] for a in actions] == ["error"]
        row = await conn.fetchrow(
            "SELECT next_run_at, run_count, last_execution_id "
            "FROM kernel.errand_schedules WHERE id = $1", sid)
        assert row["next_run_at"] is not None, "the schedule was retired"
        assert row["last_execution_id"] is None
    _run(body)


def test_an_unusable_cron_pauses_the_schedule_rather_than_looping():
    """An expression that cannot be satisfied will not become
    satisfiable, and a sweep recomputing it every four seconds logs the
    same error for ever."""
    from kernel.worker.schedules import advance

    async def body(conn):
        sid = await conn.fetchval(
            "INSERT INTO kernel.errand_schedules "
            "(goal_id, task, cron, next_run_at) "
            "VALUES ($1,'t','0 0 31 2 *', now()) RETURNING id",
            f"{PREFIX}-nevercron")
        await advance(conn, sid, None)
        row = await conn.fetchrow(
            "SELECT paused, next_run_at FROM kernel.errand_schedules "
            "WHERE id = $1", sid)
        assert row["paused"] is True
        assert row["next_run_at"] is None
    _run(body)


def test_the_sweep_runs_on_the_tick_that_already_exists():
    """No second scheduler. By source, because `worker_tick` needs a
    driver and a live adapter to call."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/worker/loop.py", encoding="utf-8").read()
    body = src[src.index("async def worker_tick"):]
    assert "_schedule_sweep(conn)" in body, (
        "the sweep is not on the worker tick, so something else must be "
        "running it — which is the second scheduler this avoids")


# ── I1d · a scheduled errand belongs to a story, so somebody can see it ──

def test_the_execution_carries_the_schedules_story():
    """THE Ec5 BUG. There is no thread on an execution — it is derived
    through the story, twice, by card delivery and by the ledger's
    principal. A story-less run has no card, no principal, no sidebar row
    and no controls, and an errand parks for approval on its FIRST leg.
    So every scheduled errand was invisible and unanswerable."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        goal = f"{PREFIX}-story-{os.urandom(3).hex()}"
        story = f"{PREFIX}-st-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) "
            "VALUES ($1,'t','tester')", goal)
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by) "
            "VALUES ($1,$2,'s','in_progress','tester')", story, goal)
        await conn.execute(
            "INSERT INTO kernel.errand_schedules "
            "(goal_id, story_id, task, cron, next_run_at) "
            "VALUES ($1,$2,'check the logs','*/5 * * * *', now())",
            goal, story)

        await sweep(conn)
        got = await conn.fetchval(
            "SELECT story_id FROM kernel.executions WHERE goal_id = $1", goal)
        assert got == story, "the run belongs to nobody"
    _run(body)


def test_a_schedule_with_no_story_still_runs_and_is_not_a_crash():
    """A goal-level schedule is a legitimate shape — it simply cannot
    park for a person until it has a story. Refusing it here would be a
    policy decision taken in the wrong place."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        goal = f"{PREFIX}-nostory-{os.urandom(3).hex()}"
        await _schedule(conn, goal=goal)
        actions = await sweep(conn)
        assert [a["action"] for a in actions] == ["scheduled"]
        assert await conn.fetchval(
            "SELECT story_id FROM kernel.executions WHERE goal_id = $1",
            goal) is None
    _run(body)


# ── I1e · a schedule stops ───────────────────────────────────────────────

def test_a_schedule_retires_at_its_run_ceiling():
    """`run_count` was incremented by Ec5 and read by NOTHING, so "every
    five minutes" authorised 288 runs a day for ever at roughly 10,800
    tokens a park. Under the autonomy design the approving human is the
    principal for every one of them."""
    from kernel.worker.schedules import due, sweep

    async def body(conn):
        goal = f"{PREFIX}-ceiling-{os.urandom(3).hex()}"
        sid = await _schedule(conn, goal=goal)
        await conn.execute(
            "UPDATE kernel.errand_schedules SET max_runs = 2 WHERE id = $1",
            sid)

        for expected in (1, 2):
            await conn.execute(
                "UPDATE kernel.errand_schedules SET next_run_at = now() "
                "WHERE id = $1 AND paused = false", sid)
            await sweep(conn)
            row = await conn.fetchrow(
                "SELECT run_count, paused, next_run_at, retired_reason "
                "FROM kernel.errand_schedules WHERE id = $1", sid)
            assert row["run_count"] == expected, expected

        assert row["paused"] is True
        assert row["next_run_at"] is None
        assert "ceiling of 2" in (row["retired_reason"] or "")
        # And it really is over: nothing more is due, ever.
        assert await due(conn) == []
        runs = await conn.fetchval(
            "SELECT count(*) FROM kernel.executions WHERE goal_id = $1", goal)
        assert runs == 2, f"{runs} runs against a ceiling of 2"
    _run(body)


def test_a_schedule_retires_at_its_expiry():
    """An expiry is what bounds a schedule whose run count nobody can
    predict — "watch this every five minutes until Friday"."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        sid = await _schedule(conn)
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        await conn.execute(
            "UPDATE kernel.errand_schedules SET expires_at = $2 WHERE id = $1",
            sid, past)
        await sweep(conn)
        row = await conn.fetchrow(
            "SELECT paused, next_run_at, retired_reason "
            "FROM kernel.errand_schedules WHERE id = $1", sid)
        assert row["paused"] is True and row["next_run_at"] is None
        assert "expiry" in (row["retired_reason"] or "")
    _run(body)


def test_retiring_says_why_rather_than_going_quiet():
    """A schedule that stops with no reason is indistinguishable from a
    broken sweep, and the first thing anybody does is re-enable it."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        sid = await _schedule(conn)
        await conn.execute(
            "UPDATE kernel.errand_schedules SET max_runs = 1 WHERE id = $1",
            sid)
        await sweep(conn)
        reason = await conn.fetchval(
            "SELECT retired_reason FROM kernel.errand_schedules WHERE id = $1",
            sid)
        assert reason and reason.strip(), "it went quiet with no reason"
    _run(body)


def test_an_unbounded_schedule_is_still_unbounded():
    """NULL on both is what every hand-made schedule wants, and it is the
    honest default for one a person created and watches. Only a delegated
    grant must set a bound, and that is enforced where the grant is
    written."""
    from kernel.worker.schedules import sweep

    async def body(conn):
        sid = await _schedule(conn)
        for _ in range(3):
            await conn.execute(
                "UPDATE kernel.errand_schedules SET next_run_at = now() "
                "WHERE id = $1", sid)
            await sweep(conn)
        row = await conn.fetchrow(
            "SELECT run_count, paused, next_run_at FROM "
            "kernel.errand_schedules WHERE id = $1", sid)
        assert row["run_count"] == 3
        assert row["paused"] is False and row["next_run_at"] is not None
    _run(body)


def test_a_ceiling_of_zero_is_refused_by_the_database():
    """Not a policy — a schedule that can never run is a mistake, and it
    should be refused rather than created and retired on first sweep."""
    async def body(conn):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO kernel.errand_schedules "
                    "(goal_id, task, cron, max_runs) "
                    "VALUES ($1,'t','* * * * *', 0)", f"{PREFIX}-zero")
    _run(body)
