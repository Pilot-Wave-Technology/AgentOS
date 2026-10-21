"""A progress wake costs a whole GO turn, so it has to be worth one.

`enqueue_system_wake` queues a wake FCFS as its own turn and the model
reads its context to answer it. At a 500K window that is not free, and
`[NO_REPLY]` does not refund it -- the turn already ran. So the only
lever is HOW MANY wakes there are, and every test here is about that.

THE THREE PROPERTIES THAT KEEP THE COST DOWN:

    empty digest -> no wake      a quiet errand costs nothing at all
    one per window               events coalesce instead of each buying a turn
    only what GO can act on      a person's decision is not GO's to watch

Lose any one and this becomes the heartbeat it was written not to be.
"""

from __future__ import annotations

import asyncio
import io
import os
import pathlib
import re
import uuid

import asyncpg
import pytest

from app.services.tests.shipped_tree import (
    IS_DISTRIBUTION, create_table, table_has_column)

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = io.open(ROOT / "app/services/go_errand_watch.py", encoding="utf-8").read()
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def _code(text: str) -> str:
    no_doc = re.sub(r'""".*?"""', "", text, flags=re.S)
    return "\n".join(line for line in no_doc.splitlines()
                     if not line.lstrip().startswith("#"))


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "kernel,public"}),
                timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


async def _digest(conn, row, quiet=None, window=None):
    """`digest` with the two cadence arguments the sweep computes.

    THESE TESTS WERE SKIPPING when this signature changed -- no local
    database meant `_connect_or_skip` bailed out, and nine call sites
    went on passing two arguments to a four-argument function with
    nothing to notice. They fail loudly the moment a database is
    present, which is the whole reason to run the suite against one.

    `quiet=None` is the DEFAULT here because it is the interesting
    case: a run nobody has checked in on yet, which is due immediately.
    """
    from app.services.errand_checkin import DEFAULT_S
    from app.services.go_errand_watch import digest

    return await digest(conn, row, DEFAULT_S if window is None else window,
                        quiet)


async def _errand(conn, state: str = "PARKED_HOT") -> dict:
    """A live autonomous errand with nothing wrong with it.

    PARKED_HOT BY DEFAULT, because that is the state with nothing for GO
    to do -- the run is waiting on somebody else. A RUNNING errand now
    gets a check-in every window (asked for 2026-09-24), so a fixture
    left RUNNING would make every "produces no wake" test assert the
    opposite of what it reads as.
    """
    tag = uuid.uuid4().hex[:8]
    goal, story = f"W{tag}", f"s{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','a')", goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by, "
        "  work_kind) VALUES ($1,$2,'Read the logs','in_progress','a',"
        "  'errand')", story, goal)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, state, generation,"
        "  kind, domain) VALUES ($1,$2,$3,0,'errand','ops') "
        "RETURNING id", goal, story, state)
    return {"id": eid, "goal_id": goal, "story_id": story,
            "state": state, "title": "Read the logs"}


# ── the property that matters most ───────────────────────────────────

@pytest.mark.asyncio
async def test_an_errand_waiting_on_a_person_produces_no_wake_at_all():
    """STILL THE POINT, for everything except a RUNNING run. A run
    parked on somebody else's decision has nothing for GO to do, and a
    wake would spend a turn saying so."""
    from app.services.go_errand_watch import digest

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        assert await _digest(conn, await _errand(conn)) == ""
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_plan_waiting_for_approval_is_worth_a_turn():
    from app.services.go_errand_watch import digest

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        row = await _errand(conn)
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, generation, kind, "
            "  question_text, status) "
            "VALUES ($1,0,'authorization','here is the plan','OPEN')",
            row["id"])
        body = await _digest(conn, row)
        assert "waiting for your approval" in body
        assert row["story_id"] in body
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acceptance", "direction", "permission"])
async def test_a_persons_decision_does_not_wake_go(kind):
    """GO cannot settle these. The person already has the card, and
    waking GO to watch somebody else decide is the purest form of the
    waste this module exists to avoid."""
    from app.services.go_errand_watch import digest

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        row = await _errand(conn)
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, generation, kind, "
            "  question_text, status) VALUES ($1,0,$2,'decide','OPEN')",
            row["id"], kind)
        assert await _digest(conn, row) == "", (
            f"a {kind} park woke GO, which can do nothing about it")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_talking_job_is_not_a_stall_and_a_silent_one_is():
    """`running` alone cannot separate a slow compile from a pod that
    vanished. The signal is the last byte, and the threshold is the one
    GO's own prompt names."""
    from app.services.go_errand_watch import SILENT_JOB_S, digest

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        row = await _errand(conn)
        await conn.execute(
            "INSERT INTO kernel.execution_jobs (execution_id, job_id, "
            "  command, running, started_at, last_seen_at) "
            "VALUES ($1,'j1','pytest -q',true, now() - interval '3 hours', "
            "        now() - interval '30 seconds')", row["id"])
        assert await _digest(conn, row) == "", (
            "a job producing output was reported as stalled")

        await conn.execute(
            "UPDATE kernel.execution_jobs SET last_seen_at = "
            "  now() - ($2 || ' seconds')::interval - interval '1 minute' "
            " WHERE execution_id = $1", row["id"], str(SILENT_JOB_S))
        body = await _digest(conn, row)
        assert "produced nothing" in body and "j1" in body
    finally:
        await tx.rollback()
        await conn.close()


# ── the coalescing, and where the wake lands ─────────────────────────

def test_the_wake_is_rate_limited_by_elapsed_time_not_the_clock():
    """A WALL-CLOCK BUCKET BOUNDS THE COST AND PROMISES NOTHING ABOUT
    THE GAP. `int(time.time()) // 900` allows one wake per slice of the
    day, so two consecutive wakes can be fifteen minutes apart or a few
    seconds -- observed at 3m40s on run 306, which is what made the
    feature impossible to judge by watching it.

    The gate is now time since THIS run's last wake, which is what an
    interval was always meant to mean."""
    code = _code(SRC)
    assert "errand_checkin.quiet_s(" in code
    # The comparison moved into `errand_checkin.is_due`, which also
    # decides what a run with NO check-in history is measured against
    # (its age, not "now") -- see that module.
    assert "errand_checkin.is_due(" in code, (
        "nothing compares elapsed time to the window, so the interval "
        "is not an interval")
    assert "// WINDOW_S" not in code


def test_the_ref_still_dedupes_two_replicas():
    """The elapsed gate cannot settle a race: two sweeps at the same
    moment read the same last-wake time and both say yes. The unique
    index on (goal_id, kind, ref) is what makes only one land, exactly
    as it did for the bucket."""
    code = _code(SRC)
    assert "errand_checkin.ref(row[\"id\"])" in code
    from app.services.errand_checkin import ref
    assert ref(306).startswith("306:")


def test_the_wake_lands_on_the_errands_own_thread():
    """An errand parks and asks many times over its life. Routed to the
    main GO thread they spend the window of the one conversation a
    person reads and which has to last the goal -- which is recorded in
    `_deliver`'s own docstring as a cost already paid once."""
    code = _code(SRC)
    assert "_errand_thread(pool, row[\"id\"])" in code
    assert code.index("_errand_thread") < code.index("_deliver(pool")


def test_the_silence_threshold_is_not_a_second_opinion():
    """GO's prompt says an hour. A different number here would mean the
    platform and the agent disagreed about what a stall is."""
    from app.services.go_errand_watch import SILENT_JOB_S

    assert SILENT_JOB_S == 3600
    agents = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    assert "last byte was an hour ago" in agents, (
        "GO's prompt no longer names the hour this constant mirrors")


def test_looking_often_is_not_waking_often():
    """The sweep runs on a short interval so a stall is noticed
    promptly; the WINDOW is what bounds the cost. Confusing the two is
    how this becomes a heartbeat."""
    from app.services.errand_checkin import DEFAULT_S, MIN_S
    from app.services.go_errand_watch import INTERVAL_S

    assert INTERVAL_S <= 120
    assert DEFAULT_S >= 300
    assert DEFAULT_S > INTERVAL_S
    # THE FLOOR CANNOT PROMISE MORE THAN THE LOOP DELIVERS. A window
    # below the sweep interval would be a cadence nothing implements.
    assert MIN_S >= INTERVAL_S


def test_the_human_only_kinds_are_not_listed_twice():
    """`HUMAN_ANSWERED_KINDS` lives in the kernel because "a fact about
    kernel data that the kernel does not own is a fact that drifts". A
    copy here would be that fact owned twice -- and the copy would go
    stale silently, waking GO about a park only a person can settle."""
    from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS

    from app.services.go_errand_watch import HUMAN_ONLY

    assert HUMAN_ONLY == frozenset(HUMAN_ANSWERED_KINDS) - {"authorization"}
    assert "authorization" not in HUMAN_ONLY, (
        "GO cannot be woken to approve a plan it is now allowed to approve")
    for kind in ("acceptance", "direction", "permission", "escalation"):
        assert kind in HUMAN_ONLY


def test_a_new_human_only_kind_is_excluded_automatically():
    """Derivation has to fail SAFE: a kind added to the kernel's set must
    make GO woken less, never more."""
    code = _code(SRC)
    assert "kind <> ALL($2::text[])" in code, (
        "the filter names GO-answerable kinds explicitly, so a new "
        "human-only kind would default to waking GO")


def test_only_autonomous_errands_are_watched():
    """A story has an assistant whose whole job is that story, and a
    hand-started errand has a person sitting with it. Waking GO about
    either is both waste and interference."""
    # THE SQL CONSTANT ITSELF, sliced out rather than run through
    # `_code` -- that strips triple-quoted strings as docstrings, and
    # `_LIVE` is one, so the query it is about disappeared and the test
    # passed on an empty haystack.
    sql = SRC[SRC.index('_LIVE = """'):]
    sql = sql[:sql.index('"""', 12)]
    assert "'errand'" in sql and "errand_schedules" in sql
    assert "approved_by IS NOT NULL" in sql and "revoked_at IS NULL" in sql
    assert "terminal_states" in sql, (
        "a finished errand would be woken about for ever")


def test_one_bad_errand_does_not_stop_the_sweep():
    code = _code(SRC)
    body = code[code.index("async def sweep"):]
    assert "try:" in body and "except" in body


# ── and it must run on the APP's pool, not the conductor's ───────────

@pytest.mark.asyncio
async def test_every_query_runs_with_no_kernel_in_the_search_path():
    """THE ONE THE SUITE MISSED, and it cost the whole feature.

    This module runs on the APP's pool (the scheduler service), whose
    search_path is `public` alone. `run_state_machine` was left
    unqualified -- fine in `kernel/conductor/admit.py`, where the
    conductor connects with `search_path=kernel,public`, and fatal
    here. Every tick from 2026-09-24 10:52 raised UndefinedTableError,
    the caller's guard turned it into one warning line, and the wake
    never fired once. Found by running it.

    Every other test in this file connects WITHOUT forcing a
    search_path, so none of them could see it. This one connects the
    way the scheduler does and executes the real SQL.
    """
    from app.services.go_errand_watch import _LIVE, _open_questions, \
        _silent_jobs

    conn = None
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            conn = await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "public"}), timeout=5)
            break
        except Exception:                                     # noqa: BLE001
            continue
    if conn is None:
        pytest.skip("no local database")
    try:
        # Executing is the test: an unqualified kernel table raises here
        # and nowhere else.
        await conn.fetch(_LIVE)
        await _open_questions(conn, 0)
        await _silent_jobs(conn, 0)
    finally:
        await conn.close()


def test_no_kernel_table_is_left_unqualified():
    """The same rule as a readable assertion, so a NEW query that
    forgets it names itself rather than failing at 3am inside a guard
    that hides it."""
    sql = "\n".join(
        line for line in SRC.splitlines()
        if any(w in line for w in ("FROM ", "JOIN ")))
    for table in ("executions", "run_state_machine", "errand_schedules",
                  "questions", "execution_jobs"):
        for line in sql.splitlines():
            if table in line and "kernel." + table not in line:
                raise AssertionError(
                    f"{table!r} is unqualified in: {line.strip()!r} — this "
                    f"module runs on a pool with no `kernel` in its "
                    f"search_path")


# ── and it has to run where a wake can actually be DELIVERED ─────────

def test_the_watch_runs_beside_the_other_wake_producers():
    """THE SECOND WAY THIS FEATURE DID NOTHING, found live on
    2026-09-24 after the first was fixed.

    It was wired into `kernel_park_cards`' loop -- reasonable on the
    face of it ("a separate poller is a second thing to remember to
    start") and wrong, because that loop runs on the SCHEDULER service,
    which has no `CHAT_QUEUE_ENABLED`. `enqueue_system_wake` returns
    None there, `_deliver` releases its `go_wake_log` claim, and the
    wake disappeared with no exception, no row and no log line --
    indistinguishable from the watch's normal quiet state.

    A card poster writes a row to `messages`. A wake producer enqueues a
    TURN. They need different things from the process they run in, and
    `go_wakes` is where the turn-enqueuing ones already live."""
    wakes = io.open(ROOT / "app/services/go_wakes.py", encoding="utf-8").read()
    cards = io.open(ROOT / "app/services/kernel_park_cards.py",
                    encoding="utf-8").read()
    assert "go_errand_watch" in wakes, (
        "the watch is not on the loop that can deliver a wake")
    assert "go_errand_watch" not in cards, (
        "the watch is still on the card loop, where enqueue returns None")


def test_a_dropped_wake_says_so():
    """Releasing the claim when the queue is off is RIGHT -- the
    backstop should retry. Doing it silently is what made this cost
    half an hour: correct computation, dropped delivery, no trace."""
    src = io.open(ROOT / "app/services/go_wake_events.py",
                  encoding="utf-8").read()
    branch = src[src.index("if msg_id is None:"):]
    branch = branch[:branch.index("return False")]
    assert "_log.warning" in branch, (
        "a wake dropped for want of a queue leaves no trace at all")


# ── the 15-minute check-in on a RUNNING errand ───────────────────────
#
# The one TIMED trigger, and the exception to everything above. Asked
# for on 2026-09-24: event-only left a long errand that is progressing
# normally but heading somewhere useless producing no wake at all, and
# GO cannot steer what it never looks at.

@pytest.mark.asyncio
async def test_a_running_errand_is_checked_on_every_window():
    """Nothing wrong with it, nothing asked -- and GO is still woken,
    because "going fine" and "going somewhere useful" are different
    questions and only one of them raises an event."""
    from app.services.go_errand_watch import digest

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        body = await _digest(conn, await _errand(conn, state="RUNNING"))
        assert body, "a running errand is never checked on"
        assert "nothing has been reported" in body
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_check_in_says_ASK_and_not_steer():
    """An executor forty steps in knows things the journal does not. A
    steer sent before asking is a guess with authority behind it, and
    GO's prompt already says so -- but the instruction rides in the
    wake because this is the moment it applies."""
    from app.services.go_errand_watch import digest

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        body = await _digest(conn, await _errand(conn, state="RUNNING"))
        assert "ask_executor" in body
        # THE WORDING MOVED, THE RULE DID NOT. The line used to read
        # "`ask_executor`, NOT `steer`"; it is now a numbered sequence
        # ending "ASK, DO NOT STEER", because the preference alone did
        # not hold on runs 307 and 308 -- both read the journal instead.
        assert "against the approved plan" in body
        assert "story_progress" in body, (
            "the fallback is unnamed, so GO has to guess what to do "
            "when the ask cannot be answered")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_parked_run_is_not_checked_on():
    """It is waiting on somebody else's decision. There is nothing to
    ask the executor, and waking GO to watch is the waste the whole
    module exists to avoid."""
    from app.services.go_errand_watch import digest

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        for state in ("PARKED_HOT", "PARKED_COLD"):
            row = await _errand(conn, state=state)
            assert await _digest(conn, row) == "", state
    finally:
        await tx.rollback()
        await conn.close()


def test_fifteen_minutes_is_the_default_and_only_the_default():
    """Four turns an hour per running errand is the stated price, and
    the number is now an operator setting -- seeded at the constant it
    replaces, so applying the migration changes no behaviour."""
    from app.services.errand_checkin import DEFAULT_S

    assert DEFAULT_S == 900
    if IS_DISTRIBUTION:
        # The distribution seeds no row; an unset key must fall back to
        # DEFAULT_S, so a fresh install runs at the same 900.
        chk = io.open(ROOT / "app/services/errand_checkin.py",
                      encoding="utf-8").read()
        assert re.search(r"get_int\(\s*platform_settings\.CHECKIN_SECONDS,"
                         r"\s*DEFAULT_S\)", chk)
        svc = io.open(ROOT / "app/services/platform_settings.py",
                      encoding="utf-8").read()
        assert "return default if raw is None else int(raw)" in svc
    else:
        sql = io.open(ROOT / "migrations/errand_checkin_interval.sql",
                      encoding="utf-8").read()
        assert "'errand_checkin_seconds', NULL, 900" in sql


def test_a_typo_in_the_settings_box_cannot_switch_it_off():
    """`9000000` for `900` would silence every errand and look like the
    feature simply not working -- the one failure a free-text number
    invites, and the reason both ends are clamped."""
    from app.services.errand_checkin import MAX_S, MIN_S

    assert MAX_S <= 86_400
    assert MIN_S >= 60
    src = io.open(ROOT / "app/services/errand_checkin.py",
                  encoding="utf-8").read()
    assert "max(MIN_S, min(MAX_S, raw))" in src


def test_the_setting_is_a_number_and_not_a_switch():
    """`platform_settings.value` is BOOLEAN. Reading an interval
    through it would give `true`, and a caller that asked for a switch
    and got 900 would read it as on -- every one of those callers is a
    guard."""
    svc = io.open(ROOT / "app/services/platform_settings.py",
                  encoding="utf-8").read()
    assert "async def get_int(" in svc and "value_int" in svc
    if IS_DISTRIBUTION:
        assert table_has_column("platform_settings", "value_int")
        assert re.search(r"^\s+value boolean,$",
                         create_table("platform_settings"), re.M), (
            "platform_settings.value must be nullable for numeric keys")
    else:
        mig = io.open(ROOT / "migrations/errand_checkin_interval.sql",
                      encoding="utf-8").read()
        assert "ADD COLUMN IF NOT EXISTS value_int" in mig
        assert "ALTER COLUMN value DROP NOT NULL" in mig


def test_the_first_check_in_waits_out_the_window_like_every_other():
    """REVERSES A DELIBERATE DECISION, so it records why.

    The old rule was "a run with no prior wake is due now", chosen to
    make "does it fire when the errand starts?" answerable. It did that
    by making the first check-in land while the run was still being
    dispatched -- and the first check-in is the one look GO takes at a
    run that has, necessarily, done nothing yet.

    RUN 345 IS THE COST. Window configured at 600s; first check-in at 28
    seconds; the pod was Unschedulable, which the conductor treats as
    normal for ninety; GO was told the run had stalled and cancelled it
    at eighty. Honouring the window puts the first look at ten minutes,
    by which time the pod is placed and there is nothing to report.

    Promptness was never the point of a check-in. Noticing a long run
    going somewhere useless is, and that needs the run to be running."""
    code = _code(SRC)
    assert "errand_checkin.is_due(quiet, row[\"age_s\"], window)" in code, (
        "the first check-in is due immediately again, which is how run "
        "345 was cancelled while its pod was still being scheduled")


def test_the_prompt_teaches_the_check_in_and_ask_not_steer():
    """A wake GO has never heard of is a wake it reasons about from
    first principles every time. And the ask-not-steer rule has to be
    in BOTH places: the wake says it at the moment it applies, the
    prompt says it so GO is not learning the rule from the interrupt."""
    md = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    # NOT A LITERAL NUMBER. The cadence is an operator setting, so a
    # prompt naming fifteen minutes would be wrong the moment anybody
    # changed it -- and changing it would then need a prompt re-push
    # to every GO on the platform, which is not a knob anybody would
    # turn. The wake carries the measured figure instead.
    assert "checked on a regular cadence" in md
    # Wrapped text: assert on a fragment that does not cross a line.
    assert "an operator can set" in md
    # SAME RULE, STRONGER WORDING. It was "ASK — do not steer";
    # the section is now a numbered sequence that puts
    # `ask_executor` first and names `story_progress` as the
    # fallback, because the preference alone did not hold.
    assert "ASK, DO NOT STEER" in md
    assert "1. **Call `ask_executor`.**" in md
    assert "ask_executor" in md
    # And the old absolute must be gone: silence is only safe for a
    # PARKED run now, because a RUNNING one is checked on a timer.
    assert "Silence means nothing is wrong** for a run that is PARKED" in md
