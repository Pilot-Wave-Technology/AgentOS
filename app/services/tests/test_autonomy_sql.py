"""I5b/I5c · who drives an autonomous errand, and who authorised it.

THE DECISION THIS ENCODES: **humans own it.** The approving human is the
principal for every autonomous run of the grant they approved — their
thread drives it, their ledger principal, their cost — for the grant's
life. The alternatives were rejected: attributing to the goal loses the
audit chain back to a person, and re-asking periodically is not autonomy.

Two consequences follow and are tested as requirements rather than
preferences. `SYSTEM_ACTOR` is WRONG for an autonomous run — a standing
grant is a human deciding in advance, not the platform deciding, and
attributing it to the system would re-introduce the over-attribution
regression that was already fixed once. And `max_runs`/`expires_at` are
load-bearing: a person is accountable for every run, so the number of runs
they are accountable for must be bounded.

THE RULE THAT MUST NOT WEAKEN is that approving is a human act. A standing
grant does not weaken it, and this file is where that claim is checked:
the grant carries a real human uid, no agent can write one, and it is
pinned to a single plan hash — so an errand that re-plans falls back to
asking rather than inheriting autonomy.

Real Postgres, one transaction, always rolled back.
"""
from __future__ import annotations

import pathlib
import asyncio
import datetime as dt
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

from kernel.assistant.context import Refused  # noqa: E402

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-auto-test"


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
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='executions' AND column_name='driving_thread_id'")
        if ok:
            return conn
        await conn.close()
    pytest.skip("no local database with driving_thread_id")



def _park_family() -> str:
    """Every `kernel/harness/park*.py`, concatenated.

    THE FACADE IS NOT THE CODE. `park.py` was 519 lines until wave 4a
    split it into `park_kinds`, `park_open`, `park_read`,
    `park_messages`, `park_resolve` and `park_approval`; what is left is
    a 70-line re-export so the twenty-three call sites did not have to
    change. A test that reads the facade as SOURCE finds a list of
    names and none of the code it is asserting about.

    These tests live in `*_sql.py`, which `-k "not sql"` deselects --
    the run everyone uses, because the sql suites need three DSNs and
    take 27 minutes against 7. So they were stale AND silent until CI
    ran them. Plan 11.5f records the same class twice already.

    A GLOB, not a list of modules: D3 splits these files again, and a
    list goes stale the next time. A glob cannot be emptied by moving
    code inside the family.
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    return chr(10).join(
        p.read_text(encoding="utf-8")
        for p in sorted((root / "kernel" / "harness").glob("park*.py")))


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


async def _fixture(conn):
    """A goal, two people, two threads, a story and a run."""
    tag = os.urandom(4).hex()
    goal, story = f"{PREFIX}-g-{tag}", f"{PREFIX}-s-{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')",
        goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'s','accepted','alice')", story, goal)
    # EXPLICIT TIMESTAMPS, because `now()` is the TRANSACTION's start
    # time in Postgres — two rows inserted here would carry identical
    # `created_at` and the resolver's `ORDER BY created_at DESC` would
    # pick between them arbitrarily. The same trap that made a supersede
    # chain fork in Ec2f. bob is deliberately the newest, so "the old
    # resolver picks bob" is a fact rather than a coin flip.
    threads = {}
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    for offset, who in enumerate(("alice", "bob")):
        tid = f"{PREFIX}-th-{who}-{tag}"
        await conn.execute(
            "INSERT INTO threads (id, goal_id, created_by, title, story_id, "
            "created_at) VALUES ($1,$2,$3,$4,$5,$6)",
            tid, goal, who, f"{who}'s", story,
            base + dt.timedelta(minutes=offset))
        threads[who] = tid
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, state)"
        " VALUES ('errand','ops',$1,$2,'RUNNING') RETURNING id", goal, story)
    return {"goal": goal, "story": story, "threads": threads, "eid": eid}


# ── I5b · the driving thread ─────────────────────────────────────────────

def test_the_principal_follows_the_driving_thread_not_the_newest():
    """WITHOUT THIS the ledger principal is whoever imported the story
    last. With two people in a goal that is not a decision anybody made —
    and under this feature it decides who is accountable for an
    autonomous run and who pays for it."""
    async def body(conn):
        from kernel.ledger.actors import principal_for

        f = await _fixture(conn)
        # bob's thread is newest, so the old resolver picks bob.
        got = await principal_for(conn, f["eid"])
        assert got["human_uid"] == "bob"

        await conn.execute(
            "UPDATE kernel.executions SET driving_thread_id = $2 WHERE id = $1",
            f["eid"], f["threads"]["alice"])
        got = await principal_for(conn, f["eid"])
        assert got["human_uid"] == "alice", (
            "the approving human is not the principal")
        assert got["thread_id"] == f["threads"]["alice"]
    _run(body)


def test_a_run_without_one_resolves_exactly_as_before():
    """THE STORY PATH. NULL must mean "resolve the old way" or every
    existing run changes principal."""
    async def body(conn):
        from kernel.ledger.actors import principal_for

        f = await _fixture(conn)
        assert await conn.fetchval(
            "SELECT driving_thread_id FROM kernel.executions WHERE id=$1",
            f["eid"]) is None
        got = await principal_for(conn, f["eid"])
        assert got["human_uid"] == "bob"
        assert got["thread_id"] == f["threads"]["bob"]
    _run(body)


def test_the_card_goes_to_the_driving_thread():
    """A card in a thread nobody is watching is in the database, correct
    and invisible — which is what the resolver's own comment says about
    the last time this went wrong."""
    async def body(conn):
        from app.services.kernel_park_cards import _pending

        f = await _fixture(conn)
        await conn.execute(
            "UPDATE kernel.executions SET driving_thread_id = $2 WHERE id = $1",
            f["eid"], f["threads"]["alice"])
        await conn.execute(
            "INSERT INTO kernel.questions "
            "(execution_id, kind, question_text, payload, status, generation) "
            "VALUES ($1,'acceptance','done?','{\"summary\":\"s\"}'::jsonb,"
            "'OPEN',0)", f["eid"])

        rows = [r for r in await _pending(conn)
                if r["execution_id"] == f["eid"]]
        assert rows, "the card was not found at all"
        assert rows[0]["thread_id"] == f["threads"]["alice"]
    _run(body)


def test_every_execution_scoped_resolver_prefers_it():
    """There were FIVE copies of "which thread owns this run" and one
    more would have been silently wrong. Four have an execution in scope
    and must all prefer the driving thread; the fifth is keyed on
    `stories` alone and has nothing to prefer."""
    import io
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "app/services/kernel_park_cards.py",
                  encoding="utf-8").read()
    missing = []
    for m in re.finditer(r"COALESCE\(\n.*?AS thread_id", src, re.S):
        body = m.group(0)
        if "e.story_id" in body and "e.driving_thread_id" not in body:
            missing.append(src[:m.start()].count("\n") + 1)
    assert not missing, (
        f"thread resolvers at lines {missing} ignore driving_thread_id")


# ── I5c · the standing grant ─────────────────────────────────────────────

async def _grant(conn, f, plan_hash, **kw):
    return await conn.fetchval(
        """
        INSERT INTO kernel.errand_schedules
            (goal_id, story_id, task, cron, approved_by, approved_at,
             approved_plan_hash, driving_thread_id, max_runs, expires_at,
             revoked_at)
        VALUES ($1,$2,'t','*/5 * * * *',$3, now(), $4, $5, $6, $7, $8)
        RETURNING id
        """,
        f["goal"], kw.get("story", f["story"]),
        kw.get("approved_by", "alice"), plan_hash,
        f["threads"]["alice"], kw.get("max_runs", 10),
        kw.get("expires_at"), kw.get("revoked_at"))


async def _plan_question(conn, eid, plan_hash, answered=False):
    """The plan the run registered.

    `answered` matters: `questions_one_open` allows ONE open question per
    execution, so an errand still parked on its plan cannot also park on
    an acceptance. Any test that reaches completion must have the plan
    settled first, which is what happens in reality — the errand does not
    work until somebody answers it.
    """
    await conn.execute(
        "INSERT INTO kernel.questions (execution_id, kind, question_text, "
        "payload, status, generation, content_hash, verdict, answer_text, "
        "answered_by, answered_at) "
        "VALUES ($1,'authorization','plan?','{\"steps\":[\"a\"]}'::jsonb,"
        "$3,0,$2,$4,$5,$6,$7)",
        eid, plan_hash,
        "ANSWERED" if answered else "OPEN",
        "approve" if answered else None,
        "go ahead" if answered else None,
        "alice" if answered else None,
        dt.datetime.now(dt.timezone.utc) if answered else None)


def test_a_grant_approves_the_plan_it_was_pinned_to():
    """THE FEATURE. Approved once, and the run proceeds without asking."""
    async def body(conn):
        from kernel.harness.park import plan_is_approved

        f = await _fixture(conn)
        await _plan_question(conn, f["eid"], "hash-abc")
        assert await plan_is_approved(conn, f["eid"]) is False

        await _grant(conn, f, "hash-abc")
        assert await plan_is_approved(conn, f["eid"]) is True
    _run(body)


def test_a_replanned_errand_cannot_inherit_autonomy():
    """THE SAFETY PROPERTY. `plan_hash` covers steps + rationale + CHECKS,
    so a re-plan is a different hash. Without this pin an errand could
    quietly weaken its own definition of done and then mark itself
    finished, with selfcheck — measuring checks no human re-read for that
    run — as the only thing in the way."""
    async def body(conn):
        from kernel.harness.park import plan_is_approved

        f = await _fixture(conn)
        await _grant(conn, f, "hash-abc")
        await _plan_question(conn, f["eid"], "hash-DIFFERENT")
        assert await plan_is_approved(conn, f["eid"]) is False, (
            "a plan nobody approved ran unattended")
    _run(body)


def test_an_unpinned_grant_matches_nothing():
    """A blank cheque is refused rather than trusted."""
    async def body(conn):
        from kernel.harness.park import plan_is_approved

        f = await _fixture(conn)
        await _plan_question(conn, f["eid"], "hash-abc")
        await _grant(conn, f, None)
        assert await plan_is_approved(conn, f["eid"]) is False
    _run(body)


@pytest.mark.parametrize("field,value", [
    ("revoked_at", "now"),
    ("expires_at", "past"),
])
def test_a_grant_that_is_over_is_not_a_grant(field, value):
    """Read fresh per leg, so revoking takes effect between legs — which
    is the only place it can."""
    async def body(conn):
        from kernel.harness.park import plan_is_approved

        f = await _fixture(conn)
        await _plan_question(conn, f["eid"], "hash-abc")
        when = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(seconds=1))
        await _grant(conn, f, "hash-abc", **{field: when})
        assert await plan_is_approved(conn, f["eid"]) is False
    _run(body)


def test_another_storys_grant_does_not_cover_this_run():
    async def body(conn):
        from kernel.harness.park import plan_is_approved

        f = await _fixture(conn)
        await _plan_question(conn, f["eid"], "hash-abc")
        await _grant(conn, f, "hash-abc", story=f"{PREFIX}-elsewhere")
        assert await plan_is_approved(conn, f["eid"]) is False
    _run(body)


# ── the rule that must not weaken ────────────────────────────────────────

def test_a_grant_must_name_a_human():
    """The uid is RECORDED ONCE, by a person, rather than synthesised per
    run. That is what keeps "approving is a human act" literally true."""
    async def body(conn):
        f = await _fixture(conn)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO kernel.errand_schedules "
                    "(goal_id, task, cron, approved_at, max_runs) "
                    "VALUES ($1,'t','* * * * *', now(), 5)", f["goal"])
    _run(body)


def test_a_grant_no_longer_needs_a_run_ceiling():
    """REVERSES A DELIBERATE CONSTRAINT, so it records the argument.

    `errand_schedules_grant_is_bounded` required `max_runs` or
    `expires_at` on any approved grant, because "a person clicked
    approve" meant "a person is accountable for N runs" and N had to
    exist. That was right when a cron could fire an errand 288 times a
    day without anybody asking again.

    RECURRENCE IS GONE, which is what changes it. Nothing fires an
    errand on its own; every run after the first is somebody deliberately
    asking. A ceiling now protects against something that cannot happen,
    while costing the case it was never aimed at -- run 345 was pinned to
    `max_runs=1`, spent that one run on a pod that never got scheduled,
    and could not be run again.

    WHAT STILL BOUNDS IT is in the next test."""
    async def body(conn):
        f = await _fixture(conn)
        await conn.execute(
            "INSERT INTO kernel.errand_schedules "
            "(goal_id, task, cron, approved_by, approved_at) "
            "VALUES ($1,'t','@once','alice', now())", f["goal"])
    _run(body)


def test_what_bounds_an_unbounded_grant():
    """Four things, none of them a count: it is revocable, it may carry
    an expiry, `due` will not claim it while a run of its story is live,
    and no run starts unless somebody asks for one.

    A ceiling of N was the weakest of the five and the only one that
    also blocked running the fast tests again tomorrow."""
    async def body(conn):
        f = await _fixture(conn)
        sid = await conn.fetchval(
            "INSERT INTO kernel.errand_schedules "
            "(goal_id, task, cron, approved_by, approved_at) "
            "VALUES ($1,'t','@once','alice', now()) RETURNING id", f["goal"])
        # revocable
        await conn.execute(
            "UPDATE kernel.errand_schedules SET revoked_at = now() "
            " WHERE id = $1", sid)
        assert await conn.fetchval(
            "SELECT revoked_at IS NOT NULL FROM kernel.errand_schedules "
            " WHERE id = $1", sid)
        # and a zero ceiling is still a typo, not a policy
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE kernel.errand_schedules SET max_runs = 0 "
                    " WHERE id = $1", sid)
    _run(body)


def test_a_hand_made_schedule_needs_no_bound():
    """Only a DELEGATED grant must be bounded. A schedule somebody made
    and watches is a different thing."""
    async def body(conn):
        f = await _fixture(conn)
        await conn.execute(
            "INSERT INTO kernel.errand_schedules (goal_id, task, cron) "
            "VALUES ($1,'t','* * * * *')", f["goal"])
    _run(body)


def test_no_agent_can_write_a_grant():
    """THE ENFORCEMENT IS A DATABASE PRIVILEGE, not a code path. The
    worker — which is what an agent's tools run as — holds SELECT and
    UPDATE on this table and never INSERT, so GO cannot mint its own
    standing approval however it is asked to."""
    async def body(conn):
        rows = await conn.fetch(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema='kernel' AND table_name='errand_schedules' "
            "AND grantee='agentos_worker'")
        held = {r["privilege_type"] for r in rows}
        assert "INSERT" not in held, (
            "an agent can mint its own standing approval")
        assert "SELECT" in held, "the worker cannot read grants"
    _run(body)


def test_resolve_request_was_not_touched():
    """The tempting shortcut is passing a fabricated human_uid into
    `resolve_request` from the worker. That is the one change that
    genuinely breaks §1.3, and the whole design exists to avoid it."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = _park_family()
    start = src.index("async def resolve_request")
    # TO THE END OF THE FUNCTION, not the end of the file:
    # `standing_approval` is defined further down and a slice to EOF
    # would find it there and report a routing-around that has not
    # happened.
    # END OF FAMILY IS A VALID BOUND. The first version used
    # `.index`, which RAISES when `resolve_request` is the last
    # async def in the concatenation -- and after the park split it
    # is, because `park_resolve.py` sorts last but one and nothing
    # defines an async function after it. A missing terminator is
    # "the function runs to the end", not an error.
    nxt = src.find(chr(10) + "async def ", start + 1)
    if nxt < 0:
        nxt = len(src)
    body = src[start:nxt]
    assert "E_APPROVAL_NOT_HUMAN" in body, "the human-uid guard is gone"
    assert "standing_approval(" not in body, (
        "resolve_request now consults the grant — the guard has been "
        "routed around rather than left alone")

    # I5f — WRITING THE GRANT IS NOT READING IT, and the difference is
    # the whole rule. `pin_plan_to_grant` records the plan a person just
    # approved onto a grant that was waiting for one; it cannot let a
    # grant stand in for a person, because it runs only where a person
    # has already been established.
    #
    # Checked positionally rather than trusted: the human guard must
    # come FIRST in this function. A pin that ran before it would write
    # a person's authority onto a plan no person approved.
    if "pin_plan_to_grant" in body:
        assert (body.index("E_APPROVAL_NOT_HUMAN")
                < body.index("pin_plan_to_grant")), (
            "the grant is pinned before the human check — an agent's "
            "approval would write itself onto the grant")
        pin = body[body.index("pin_plan_to_grant"):]
        assert "conn, request_id" in pin[:60], (
            "the pin takes something other than the question it is "
            "recording; it must derive the hash from that row alone")


def test_a_driving_thread_from_another_story_still_wins():
    """THE CASE THE `WHERE` CLAUSE IS ACTUALLY FOR, and the one an
    autonomous errand hits.

    An ordering preference alone is not enough: it only reorders threads
    that already matched, and every thread matching `th.story_id =
    e.story_id` is by definition on this story. A grant's driving thread
    is the approving human's assistant thread in the GOAL — it need not
    be attached to this errand's story at all, and often is not, because
    the errand was proposed by GO rather than opened by that person.

    Without the WHERE arm the resolver would find nothing and fall back
    to the story's newest thread, silently making somebody else the
    principal for a run they never approved.
    """
    async def body(conn):
        from kernel.ledger.actors import principal_for

        f = await _fixture(conn)
        # A thread of carol's, in the goal, on no story at all.
        carol = f"{PREFIX}-th-carol-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO threads (id, goal_id, created_by, title) "
            "VALUES ($1,$2,'carol','carol''s')", carol, f["goal"])
        await conn.execute(
            "UPDATE kernel.executions SET driving_thread_id = $2 WHERE id = $1",
            f["eid"], carol)

        got = await principal_for(conn, f["eid"])
        assert got["human_uid"] == "carol", (
            "a run driven by a thread outside its story fell back to the "
            "story's newest — making somebody else accountable")
        assert got["thread_id"] == carol
    _run(body)


# ── I5d · a grant means the person answered in advance ───────────────────

class _Driver:
    async def exec(self, container_id, tool, args):
        return {"exit_code": 0, "output": "ok"}


async def _errand_ready_to_finish(conn, f, plan_hash="hash-abc"):
    """An errand whose plan is registered and whose checks will hold."""
    from kernel.worker.contracts import new_version

    await new_version(conn, f["eid"],
                      [{"expect": "artifact", "role": "report",
                        "label": "goal"}], "a plan", "tester")
    # ANSWERED: an errand parked on its plan is not working, and
    # `questions_one_open` would refuse the acceptance park anyway.
    await _plan_question(conn, f["eid"], plan_hash, answered=True)
    # A ROW THAT CORRESPONDS TO BYTES. The manifest row used to be
    # written alone, with a made-up id ('sha-r') and no object behind
    # it, and the artifact check passed on it -- which is exactly the
    # defect that let five real artifacts be catalogued and unreadable
    # for two days. Now the check asks the store, so a fixture that
    # lies about a deliverable fails like the real thing would.
    #
    # These tests are about AUTONOMY, not storage: they need a genuinely
    # satisfied check so the grant is what decides the outcome.
    from kernel.artifacts.store import get_store

    body = b"# Report: found it"
    artifact_id = get_store().put(body)
    await conn.execute(
        "INSERT INTO kernel.artifact_manifest "
        "(execution_id, artifact_id, role, name, summary, bytes) "
        "VALUES ($1,$2,'report','r.md','found it',$3)",
        f["eid"], artifact_id, len(body))
    return {"id": f["eid"], "container_id": None}


@pytest.fixture(autouse=True)
def _a_real_artifact_store(tmp_path):
    """A store these tests can actually write to.

    The artifact check reads bytes back now, so `/artifacts` -- the
    default root, which does not exist in CI -- would fail every
    criterion here for a reason that has nothing to do with autonomy.
    Same shape as `test_artifact_fetch.py`.
    """
    from kernel.artifacts.store import FilesystemStore, set_store

    set_store(FilesystemStore(str(tmp_path / "artifacts")))
    yield
    set_store(None)


def test_a_granted_errand_STILL_ASKS_BEFORE_IT_CLOSES():
    """THIS TEST USED TO ASSERT THE OPPOSITE, and the reasoning it
    recorded was sound as far as it went: the person approved this plan,
    with these checks, and said it may finish on its own. The self-check
    had measured the claim against the plan's own checks and the grant
    was pinned to that exact plan hash.

    WHAT IT MISSED is that finishing is not only "was the plan carried
    out". It is also "is this enough?", and nobody can answer that in
    advance, because the answer depends on what the work FOUND. An
    errand that did exactly what its approved plan said can still leave
    the obvious next question unasked -- and it went DONE with nobody
    reading it.

    So the run parks on an acceptance like any other errand. The card
    has asked the right question since it was written -- "Mark story
    done" or "Ask for more work" -- and simply never reached an
    autonomous one. The GO is woken on the park and can carry the answer
    with `decide_plan` once it has asked a goal admin, which is what
    keeps this from costing what it used to.

    AUTONOMY STILL MEANS SOMETHING: a person does not have to go and
    find the card. It does not mean nobody is asked."""
    async def body(conn):
        from kernel.worker.loop import _finish_unverified

        f = await _fixture(conn)
        row = await _errand_ready_to_finish(conn, f)
        await _grant(conn, f, "hash-abc")

        got = await _finish_unverified(conn, _Driver(), row, f["eid"], 0)
        assert got["action"] == "awaiting_acceptance", got
        assert await conn.fetchval(
            "SELECT state FROM kernel.executions WHERE id=$1",
            f["eid"]) != "DONE", "it closed without being asked"
        assert await conn.fetchval(
            "SELECT count(*) FROM kernel.questions WHERE execution_id=$1 "
            "AND kind='acceptance'", f["eid"]) == 1


    _run(body)


def test_the_plan_gate_still_honours_the_grant():
    """ONLY THE COMPLETION CHANGED. Re-approving a plan a person has
    already read, every run, would be the cost autonomy was bought to
    remove -- and unlike "is this enough?", it is a question that CAN be
    answered in advance, because the grant is pinned to that exact plan
    hash."""
    import inspect

    from kernel.harness import park_approval
    assert "standing_approval" in inspect.getsource(
        park_approval.plan_is_approved)

    from kernel.worker import loop as worker_loop
    assert "standing_approval" not in inspect.getsource(
        worker_loop._finish_unverified), (
        "the completion gate honours the grant again; every errand would "
        "close with nobody reading it")


def test_the_self_check_still_runs_under_a_grant():
    """THE DIFFERENCE BETWEEN AUTONOMY AND MARKING ITS OWN HOMEWORK. A
    grant removes the second HUMAN gate, never the quality gate — a claim
    that does not meet its own plan is refused whether or not somebody
    approved the plan in advance."""
    async def body(conn):
        from kernel.worker.contracts import new_version
        from kernel.worker.loop import _finish_unverified

        f = await _fixture(conn)
        # Same plan, same grant — but nothing emitted, so the check fails.
        await new_version(conn, f["eid"],
                          [{"expect": "artifact", "role": "report",
                            "label": "goal"}], "a plan", "tester")
        await _plan_question(conn, f["eid"], "hash-abc", answered=True)
        await _grant(conn, f, "hash-abc")

        got = await _finish_unverified(
            conn, _Driver(), {"id": f["eid"], "container_id": None},
            f["eid"], 0)
        assert got["action"] == "self_check_failed", got
        assert await conn.fetchval(
            "SELECT state FROM kernel.executions WHERE id=$1",
            f["eid"]) != "DONE"
    _run(body)


def test_without_a_grant_a_person_is_still_asked():
    """THE DEFAULT, unchanged. Every errand that nobody delegated still
    parks for acceptance."""
    async def body(conn):
        from kernel.worker.loop import _finish_unverified

        f = await _fixture(conn)
        row = await _errand_ready_to_finish(conn, f)
        got = await _finish_unverified(conn, _Driver(), row, f["eid"], 0)
        assert got["action"] == "awaiting_acceptance", got
        assert await conn.fetchval(
            "SELECT state FROM kernel.executions WHERE id=$1",
            f["eid"]) != "DONE"
    _run(body)


def test_a_replanned_errand_is_asked_even_though_a_grant_exists():
    """THE PIN, END TO END. The grant covers one plan. Re-plan and the
    checks a person read are no longer the checks being measured, so the
    run must go back to a human — this is the assertion the whole design
    rests on."""
    async def body(conn):
        from kernel.worker.loop import _finish_unverified

        f = await _fixture(conn)
        row = await _errand_ready_to_finish(conn, f, plan_hash="hash-NEW")
        await _grant(conn, f, "hash-abc")        # pinned to the OLD plan

        got = await _finish_unverified(conn, _Driver(), row, f["eid"], 0)
        assert got["action"] == "awaiting_acceptance", (
            "a plan nobody approved finished itself")
    _run(body)


# ── I5e · GO watches, and GO cannot approve ──────────────────────────────
#
# "The GO should be the one actually answering and watching over the
# errand" needs GO to hold a read that shows a parked run and a write that
# unblocks one. Both already existed on the assistant's surface. What had
# to be checked is that widening the surface did not widen the AUTHORITY:
# the same call that settles a clarification must refuse an approval, and
# refuse it at the tool rather than three hops later inside a worker.


def test_go_holds_the_two_tools_in_all_three_places():
    """The N-declarations shape, which this surface has exactly. A tool in
    TOOLS but not in GO_ROUTES 404s; in GO_ROUTES but not TOOLS trips the
    startup assertion; in neither but named in the prompt is a tool GO
    tries and cannot call."""
    from kernel.assistant.api import GO_READS, GO_ROUTES, GO_WRITES
    from kernel.go import TOOLS as GO_TOOLS

    assert set(GO_ROUTES) == set(GO_TOOLS), sorted(
        set(GO_ROUTES) ^ set(GO_TOOLS))
    assert "inbox" in GO_READS and "inbox" in GO_TOOLS
    assert "relay_answer" in GO_WRITES and "relay_answer" in GO_TOOLS
    # And a read is not a write: `inbox` on POST or `relay_answer` on GET
    # is a 405, which is the split the router enforces from these sets.
    assert "inbox" not in GO_WRITES
    assert "relay_answer" not in GO_READS


def test_the_prompt_teaches_both_halves_of_the_rule():
    """A tool GO holds and is not told the limits of is worse than one it
    does not hold: it will find `relay_answer`, try it on the plan
    approval blocking its errand, and read the refusal as a bug."""
    import io
    import pathlib

    text = io.open(pathlib.Path(__file__).resolve().parents[3]
                   / "kernel/go/AGENTS.md", encoding="utf-8").read()
    assert "`inbox`" in text and "`relay_answer`" in text
    low = text.lower()
    assert "clarification" in low, "GO is not told what it MAY settle"
    # I5g CORRECTED THIS. I5e's prompt said GO must never answer a
    # question on an errand it proposed, taken from the plan's risk
    # section. That is stricter than the design asks for and stricter
    # than the mechanism needs: a `clarification` cannot approve, accept
    # or permit anything, and the kinds that can already refuse GO by
    # privilege. Forbidding it outright would have made "the GO answers
    # and watches over the errand" — the thing being built — impossible
    # on exactly the errands GO is watching.
    #
    # What the risk actually names is SILENCE, not answering. The person
    # who approved the errand owns every run of it, so GO steering one
    # has to be visible to them.
    # THE PROPERTY, NOT THE SENTENCE. This pinned the exact phrase "on
    # an errand you proposed yourself, answer", and the section was
    # rewritten when GO went from watching an errand to running one. What
    # must survive a rewrite is both halves of the rule -- answer, and be
    # seen doing it -- not the wording of either.
    assert "answering one is steering, not self-approval" in low
    assert "say that you did" in low, (
        "GO may steer its own errand invisibly — the person who approved "
        "it owns every run and cannot see it happening")
    # The refusal has to read as a design, not a fault, or GO routes
    # around it.
    #
    # THE BOUNDARY MOVED ON 2026-09-24 and this assertion moved with it.
    # It pinned "approving is a person's act", which was true of all
    # three approvals; an AUTONOMOUS errand's PLAN is now the GO's to
    # settle. What must still be taught is the line the change did not
    # cross -- a completion and a destructive command are a person's,
    # and a story is not GO's at all -- because a prompt that promised
    # more than the code allows produces a GO that tries, is refused,
    # and tells somebody the platform is broken.
    watching = low[low.index("## running an errand"):]
    assert "everything else that is an approval is still a person's" in watching
    assert "done-or-more-work at the end is never yours" in watching
    assert "a story is not yours either" in watching


def test_go_can_settle_a_clarification():
    """The whole point. An errand parked on "which column did you mean"
    is a day lost when GO knows the answer — it wrote the work."""
    async def body(conn):
        from kernel.assistant.write import relay_answer

        f = await _fixture(conn)
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'clarification','which column?','{}'::jsonb,"
            "'OPEN',0,$2) RETURNING id", f["eid"], f"clar-{f['eid']}")

        got = await relay_answer(conn, f["goal"], "go", qid,
                                 "the one named `emitted_at`",
                                 f"key-{os.urandom(3).hex()}")
        assert not isinstance(got, Refused), got
        assert got["kind"] == "answer"
    _run(body)


@pytest.mark.parametrize("kind", ["authorization", "acceptance",
                                  "permission", "escalation"])
def test_go_cannot_approve_anything(kind):
    """§1.3, from GO's side. An `authorization` is a plan; an `acceptance`
    is marking an errand done; a `permission` is `terraform destroy`.
    GO holding a tool that could settle any of them would make the human
    gate a naming convention."""
    async def body(conn):
        from kernel.assistant.write import relay_answer

        f = await _fixture(conn)
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,$3,'may I?','{}'::jsonb,'OPEN',0,$2) RETURNING id",
            f["eid"], f"{kind}-{f['eid']}", kind)

        got = await relay_answer(conn, f["goal"], "go", qid, "yes, go ahead",
                                 f"key-{os.urandom(3).hex()}",
                                 verdict="approve")
        # AT THE TOOL, synchronously. A refusal that happened later inside
        # the worker would leave GO believing it had approved, and the
        # only trace would be a log line nobody reads.
        assert isinstance(got, Refused), f"GO approved a {kind}"
        assert got.code == "E_CANNOT_APPROVE"
        # And the refusal must not name the wrong actor: it said "an
        # assistant may not approve", which reads to GO as somebody
        # else's rule.
        assert "assistant" not in got.message.lower(), got.message
    _run(body)


def test_the_approval_kinds_still_refuse_at_the_seam_too():
    """Belt and braces, and not redundant: the tool guard reads a verdict
    string, and a caller that omits the verdict entirely gets past it.
    `resolve_request` is the seam where the rule is actually true, and it
    judges by the question's KIND rather than by what the caller said."""
    import io
    import pathlib

    src = _park_family()
    assert "E_APPROVAL_NOT_HUMAN" in src
    assert "is_plan or is_acceptance" in src
    assert "is_escalation or is_permission" in src


def test_go_answering_is_attributed_to_go_and_never_to_a_person():
    """A proposal from GO carrying a human's uid would be the whole
    guard defeated in one field. The actor is what it is."""
    async def body(conn):
        from kernel.assistant.write import relay_answer

        f = await _fixture(conn)
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'clarification','which?','{}'::jsonb,'OPEN',0,$2) "
            "RETURNING id", f["eid"], f"c2-{f['eid']}")
        got = await relay_answer(conn, f["goal"], "go", qid, "that one",
                                 f"key-{os.urandom(3).hex()}")
        actor = await conn.fetchval(
            "SELECT actor_id FROM kernel.proposals WHERE id = $1",
            got["proposal"])
        assert actor == "go"
        assert not actor.startswith("human:")
    _run(body)
