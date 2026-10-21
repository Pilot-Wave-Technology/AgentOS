"""I5f · GO asks, a person in the goal grants, the errand becomes real.

THE BOUNDARY THIS FILE IS ABOUT. GO writes a proposal, which carries no
authority. A person turns it into a grant, and only a person can:
`errand_schedules` INSERT is `agentos_human` and every kernel tool call
wears `agentos_worker`. Two tables because they have two owners, and the
privilege — not the code path — is what keeps them apart.

WHAT APPROVING PRODUCES, in one transaction: a story (so every control
the UI has can find it — I1d learned that a run without one is invisible),
a schedule carrying the approver's uid and their assistant thread as
`driving_thread_id`, and the proposal pointed at both.

WHAT IT DELIBERATELY DOES NOT PRODUCE: a pinned plan. At approval time
the errand has never run, so `approved_plan_hash` is NULL and
`standing_approval` — which requires a non-NULL hash — covers nothing.
The first run parks for plan approval like any other. That is the
fail-closed direction: half-authorised grants nothing.

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
PREFIX = "zz-prop-test"


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
        if await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='kernel' "
                "AND table_name='errand_proposals'"):
            return conn
        await conn.close()
    pytest.skip("no local database with kernel.errand_proposals")



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


async def _goal(conn, with_thread=True):
    tag = os.urandom(4).hex()
    goal = f"{PREFIX}-g-{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')",
        goal)
    out = {"goal": goal, "tag": tag, "threads": {}}
    if with_thread:
        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        for offset, who in enumerate(("alice", "bob")):
            tid = f"{PREFIX}-th-{who}-{tag}"
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, title, "
                "created_at) VALUES ($1,$2,$3,$4,$5)",
                tid, goal, who, f"{who}'s", base + dt.timedelta(minutes=offset))
            out["threads"][who] = tid
    return out


# ── GO's side: an ask that grants nothing ────────────────────────────────

def test_go_can_propose_and_it_grants_nothing_yet():
    async def body(conn):
        from kernel.go.errands import propose_errand

        g = await _goal(conn)
        got = await propose_errand(
            conn, g["goal"], task="read yesterday's error log and report",
            rationale="the error rate doubled on Tuesday and nobody is "
                      "watching it daily",
            resources=[])
        assert not isinstance(got, Refused), got
        row = await conn.fetchrow(
            "SELECT * FROM kernel.errand_proposals WHERE id = $1",
            got["proposal"])
        assert row["state"] == "PENDING"
        assert row["proposed_by"] == "go"
        # A one-shot bounds itself, and GO cannot say otherwise.
        # `@once` IS "NO TIMER", NOT "ONE RUN". The ceiling used to be
        # written here as 1, which is where run 345's "the grant was for
        # 1 run" came from; an errand stays approved and can be run
        # again. Nothing schedules it -- that is what @once still means.
        assert row["cron"] == "@once" and row["max_runs"] is None
        # AND NO SCHEDULE EXISTS. This is the whole point: proposing is
        # not granting, and nothing runs from a proposal.
        assert row["schedule_id"] is None
        assert 0 == await conn.fetchval(
            "SELECT count(*) FROM kernel.errand_schedules WHERE goal_id = $1",
            g["goal"])
    _run(body)


def test_the_worker_role_cannot_decide_its_own_proposal():
    """THE PRIVILEGE, not the absence of a tool. GO has INSERT and SELECT
    on proposals and no UPDATE, so even code that tried could not approve
    one, mark a declined proposal pending again, or point one at a
    schedule it did not earn."""
    async def body(conn):
        g = await _goal(conn)
        pid = await conn.fetchval(
            "INSERT INTO kernel.errand_proposals (goal_id, proposed_by, "
            "task, rationale, max_runs) VALUES ($1,'go','t','r',1) "
            "RETURNING id", g["goal"])
        sp = conn.transaction()
        await sp.start()
        try:
            await conn.execute("SET LOCAL ROLE agentos_worker")
            with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "UPDATE kernel.errand_proposals SET state = 'APPROVED', "
                    "decided_by = 'alice', decided_at = now() WHERE id = $1",
                    pid)
        finally:
            await sp.rollback()
    _run(body)


@pytest.mark.parametrize("kwargs,code", [
    ({"task": "", "rationale": "r"}, "E_NO_TASK"),
    ({"task": "t", "rationale": ""}, "E_NO_RATIONALE"),
    # `E_BAD_CRON`, `E_UNBOUNDED` and `E_TOO_MANY_RUNS` went with
    # recurrence: all three described a repeating grant. A cron is now
    # IGNORED rather than refused -- refusing would have GO retry with a
    # different expression, which is not the point.
    #
    # THE EXPIRY CHECKS SURVIVE, and had to be rescued: the one-shot
    # path returned BEFORE them, so making every errand a one-shot would
    # have silently removed expiry validation along with recurrence.
    ({"task": "t", "rationale": "r",
      "expires_at": "2020-01-01T00:00:00Z"}, "E_EXPIRY_IN_THE_PAST"),
    ({"task": "t", "rationale": "r",
      "expires_at": "not-a-date"}, "E_BAD_EXPIRY"),
    ({"task": "t", "rationale": "r",
      "expires_at": "2099-01-01T00:00:00Z"}, "E_HORIZON_TOO_FAR"),
])
def test_a_proposal_a_person_could_not_judge_is_refused(kwargs, code):
    """Each of these produces a card somebody would have to approve on
    faith. E_UNBOUNDED is the load-bearing one: the approver owns every
    run, so how many there are is part of the question."""
    async def body(conn):
        from kernel.go.errands import propose_errand

        g = await _goal(conn)
        got = await propose_errand(
            conn, g["goal"], **{"resources": [], **kwargs})
        assert isinstance(got, Refused), f"{kwargs} was accepted"
        assert got.code == code, f"{got.code} != {code}: {got.message}"
    _run(body)


# ── the person's side: the grant, and who owns it ────────────────────────

async def _propose(conn, goal, **kw):
    from kernel.go.errands import propose_errand

    got = await propose_errand(
        conn, goal, task=kw.pop("task", "read the log"),
        rationale=kw.pop("rationale", "nobody is watching it"),
        # `resources` is REQUIRED since the errand's story is created at
        # approval with no second call to grant in. These proposals are
        # about bounds, pinning and ownership, so an explicit empty list
        # is the honest answer -- and it is a real one, distinct from
        # omitting it, which is refused.
        resources=kw.pop("resources", []), **kw)
    assert not isinstance(got, Refused), got
    return got["proposal"]


def test_approving_creates_the_grant_the_story_and_the_driver():
    async def body(conn):
        from app.services.errand_grants import approve

        g = await _goal(conn)
        pid = await _propose(conn, g["goal"])
        out = await approve(conn, g["goal"], pid, "alice")

        sched = await conn.fetchrow(
            "SELECT * FROM kernel.errand_schedules WHERE id = $1",
            out["schedule"])
        assert sched["approved_by"] == "alice"
        assert sched["approved_at"] is not None
        # I5b — THEIRS, not the goal's newest. bob's is newer and picking
        # it would put the cards where the wrong person is watching and
        # make the wrong person the ledger principal.
        #
        # And the errand's OWN, not alice's existing conversation: a
        # thread is story-scoped, so reusing hers posted this errand's
        # cards into whatever story she was last in — which is how a
        # plan-approval card ended up in a finished errand's thread,
        # correct and invisible. `driving_thread_id` decides WHOSE.
        drove = await conn.fetchrow(
            "SELECT story_id, created_by FROM threads WHERE id = $1",
            sched["driving_thread_id"])
        assert drove is not None, "the schedule drives a thread that is gone"
        assert drove["created_by"] == "alice"
        assert drove["story_id"] == out["story"]
        assert sched["driving_thread_id"] != g["threads"]["bob"]
        assert sched["story_id"] == out["story"]
        assert sched["next_run_at"] is not None, "approved and never due"

        story = await conn.fetchrow(
            "SELECT work_kind, state, created_by FROM stories WHERE id = $1",
            out["story"])
        # Phase 3/4 — an errand with a story row is one the board shows
        # and every run control can find.
        assert story["work_kind"] == "errand"
        assert story["created_by"] == "alice"

        prop = await conn.fetchrow(
            "SELECT state, decided_by, schedule_id FROM "
            "kernel.errand_proposals WHERE id = $1", pid)
        assert prop["state"] == "APPROVED"
        assert prop["decided_by"] == "alice"
        assert prop["schedule_id"] == out["schedule"]
    _run(body)


def test_the_grant_pins_no_plan_so_the_first_run_is_still_asked():
    """THE FAIL-CLOSED HALF. At approval the errand has never run, so
    there is no plan hash to pin — and `standing_approval` requires a
    non-NULL hash. A grant that covered any plan would let a person
    approve a task title and get whatever the model later decided."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.harness.park import standing_approval

        g = await _goal(conn)
        out = await approve(conn, g["goal"],
                            await _propose(conn, g["goal"]), "alice")
        hash_ = await conn.fetchval(
            "SELECT approved_plan_hash FROM kernel.errand_schedules "
            "WHERE id = $1", out["schedule"])
        assert hash_ is None

        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, "
            "state) VALUES ('errand','ops',$1,$2,'RUNNING') RETURNING id",
            g["goal"], out["story"])
        assert await standing_approval(conn, eid) is None, (
            "an unpinned grant covered a run")
    _run(body)


def test_a_member_who_never_opened_an_assistant_can_still_approve():
    """The grant records THEM, and the errand gets a thread anyway.

    THE INTENT IS UNCHANGED: refusing here would make approval depend on
    having chatted, which is not what "any human in the goal" means.

    WHAT CHANGED IS THE THREAD, and it used to be None. `_open_story_thread`
    said in a comment that it would open one "anyway" when no agent could
    be resolved -- and could not: `threads.agent_id` is NOT NULL, so
    passing None raised, the savepoint rolled back, and the errand fell
    back to whatever thread the approver was last in. On a goal with no
    GO and a person with no assistant, that was nothing at all.

    `''` is the column's own default and the honest value -- nobody could
    be resolved -- and an unowned thread is FINDABLE: the sidebar's
    conversation list admits `agent_id = ''` explicitly. A thread its
    cards can land in beats no thread, which is what this used to get.
    """
    async def body(conn):
        from app.services.errand_grants import approve

        g = await _goal(conn, with_thread=False)
        out = await approve(conn, g["goal"],
                            await _propose(conn, g["goal"]), "carol")
        assert "carol" == await conn.fetchval(
            "SELECT approved_by FROM kernel.errand_schedules WHERE id = $1",
            out["schedule"])
        assert out["driving_thread"], "the errand got no thread at all"
        drove = await conn.fetchrow(
            "SELECT story_id, created_by, agent_id FROM threads WHERE id = $1",
            out["driving_thread"])
        assert drove["story_id"] == out["story"], (
            "the driving thread is not the errand's own")
        assert drove["created_by"] == "carol", (
            "the approver must stay the principal")
        assert drove["agent_id"] == "", (
            "an agent was invented for a goal that has none")
    _run(body)


def test_a_proposal_is_decided_once():
    async def body(conn):
        from app.services.errand_grants import ProposalError, approve, decline

        g = await _goal(conn)
        pid = await _propose(conn, g["goal"])
        await approve(conn, g["goal"], pid, "alice")
        for act in (approve, decline):
            with pytest.raises(ProposalError) as caught:
                await act(conn, g["goal"], pid, "bob")
            assert caught.value.status == 409
            assert "alice" in caught.value.detail
    _run(body)


def test_a_proposal_in_another_goal_is_404_and_not_403():
    """Same reasoning as I5a: confirming a proposal exists in a goal you
    cannot see is itself the disclosure."""
    async def body(conn):
        from app.services.errand_grants import ProposalError, approve

        mine, theirs = await _goal(conn), await _goal(conn)
        pid = await _propose(conn, theirs["goal"])
        with pytest.raises(ProposalError) as caught:
            await approve(conn, mine["goal"], pid, "alice")
        assert caught.value.status == 404
        assert "No such proposal" == caught.value.detail
    _run(body)


def test_declining_records_who_and_why():
    """A proposal that vanishes gets re-proposed next week."""
    async def body(conn):
        from app.services.errand_grants import decline

        g = await _goal(conn)
        pid = await _propose(conn, g["goal"])
        await decline(conn, g["goal"], pid, "bob", "we already page on this")
        row = await conn.fetchrow(
            "SELECT state, decided_by, decline_reason, schedule_id "
            "FROM kernel.errand_proposals WHERE id = $1", pid)
        assert row["state"] == "DECLINED"
        assert row["decided_by"] == "bob"
        assert row["decline_reason"] == "we already page on this"
        assert row["schedule_id"] is None
    _run(body)


# ── the card, and the endpoint's authorisation ───────────────────────────

def test_the_card_says_what_is_being_agreed_to():
    """Not "GO wants to run an errand". The person is agreeing to own
    every run — their thread, their ledger principal, their budget — and
    the card is the only place they learn that."""
    from app.services.errand_proposal_cards import build_card

    card = build_card({
        "id": 7, "goal_id": "g", "proposed_by": "go", "cron": "@once",
        "task": "read yesterday's error log", "name": "",
        "rationale": "the rate doubled and nobody watches it daily",
        "max_runs": 1, "expires_at": None, "sandbox_size": None})

    assert card["needs_human"] is True, (
        "no agent can settle this: errand_schedules INSERT is a human "
        "privilege, so an agent-answerable card would offer something the "
        "database refuses")
    body = card["body"]
    assert "You own it" in body
    assert "the rate doubled" in body, "the reason is not on the card"
    assert "read yesterday's error log" in body
    # Bounds language went with recurrence; "runs once" is the bound.
    assert "runs once" in body, "the bound is not on the card"
    # The plan is settled by the orchestrator now, not sent back to
    # the person on every run.
    assert "plans its work first" in body, (
        "the card implies autonomy starts immediately, which it does not")
    assert [o["value"] for o in card["options"]] == ["approve", "decline"]


def test_the_card_does_not_describe_a_recurring_errand():
    """THIS TEST USED TO ASSERT THE OPPOSITE. It checked the card showed
    a cron expression, "at most 6 runs" and an expiry -- the bounds of a
    RECURRING grant.

    Recurrence has never happened. Every grant ever written is `@once,
    max_runs=1, run_count=1`, sixteen of sixteen, and the concept is
    being removed rather than left as language describing a capability
    nobody has. A card that promised repetition was asking a person to
    agree to something more open-ended than what they were getting.

    The expiry survives, because a proposal that is never approved
    genuinely does lapse -- that one is real."""
    from app.services.errand_proposal_cards import build_card

    body = build_card({
        "id": 8, "goal_id": "g", "proposed_by": "go", "cron": "0 9 * * 1",
        "task": "t", "name": "", "rationale": "r", "max_runs": 6,
        "expires_at": dt.datetime(2026, 12, 1, 9, 0,
                                  tzinfo=dt.timezone.utc),
        "sandbox_size": None})["body"]
    assert "0 9 * * 1" not in body, "the card still shows a cron"
    assert "6" not in body.split("What it will do")[0], "still promises runs"
    assert "runs once" in body
    # The lapse date is real and stays.
    assert "2026-12-01 09:00 UTC" in body


def test_the_endpoint_requires_membership_in_the_goal():
    """"Any human IN THE GOAL may approve" — the second half is the one
    that has to be enforced, and I5a found three endpoints that took a
    goal in the path and authorised nobody against it."""
    import io
    import pathlib

    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "app/routers/kernel_errands.py", encoding="utf-8").read()
    # THE SHARED HELPER, not a fourth copy — copies are how the three park
    # endpoints came to share one hole.
    assert "from app.routers.kernel_park import _require_member" in src
    for handler in ("list_proposals", "decide_proposal"):
        body = src[src.index(f"async def {handler}"):]
        body = body[:body.index("@router", 10) if "@router" in body[10:]
                    else len(body)]
        assert "_require_member(" in body, f"{handler} authorises nobody"
    # And it acts on the SIGNED-IN uid rather than anything in the body.
    # The uid is what goes on the grant and what every autonomous run is
    # then attributed to, so reading it from the request would make
    # ownership a claim instead of a fact.
    assert 'uid = str(user.get("uid") or "")' in src
    read_from_body = [line for line in src.splitlines()
                      if "body.get(" in line]
    assert read_from_body, "the body is not read at all — test is stale"
    for line in read_from_body:
        assert any(f'body.get("{ok}"' in line
                   for ok in ("verdict", "reason")), (
            f"the endpoint reads {line.strip()!r} from the request; the "
            f"only things a caller may send are the verdict and a reason")


# ── the one-shot actually runs once ──────────────────────────────────────

def test_a_one_shot_clears_its_timer_but_keeps_its_grant():
    """`errand_schedules.cron` is NOT NULL with a non-empty CHECK, so "no
    cron" is not expressible. `@once` is the sentinel — same table, same
    sweep, same human-only INSERT — and `advance` has to know it, or the
    row reaches `next_after`, raises BadCron, and is logged as "an
    unusable cron" on a schedule that did exactly what it was meant to.

    WHAT `@once` MEANS IS "NO TIMER", NOT "NEVER AGAIN". It used to
    retire the schedule on its first run, which conflated the two: run
    345 was cancelled eighty seconds in, while its pod was still being
    scheduled, and the errand was already marked "ran once, as proposed"
    and paused. It had spent its only run on work that never started,
    and `run_control` answered E_NO_RUN because there was nothing in
    flight and nothing left to start.

    So the timer is cleared -- a NULL `next_run_at` is already "not due"
    -- and the grant is left alone. Asking for another run sets the time
    again; `due` decides when it may actually start."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.worker.schedules import advance

        g = await _goal(conn)
        out = await approve(conn, g["goal"],
                            await _propose(conn, g["goal"]), "alice")
        await advance(conn, out["schedule"], 4242)

        row = await conn.fetchrow(
            "SELECT next_run_at, paused, retired_reason, run_count, "
            "last_execution_id FROM kernel.errand_schedules WHERE id = $1",
            out["schedule"])
        assert row["next_run_at"] is None, "a one-shot is due again"
        # NOT RETIRED, AND NOTHING IS HIDDEN BY THAT. The old code wrote
        # `paused = true, retired_reason = 'ran once, as proposed'`,
        # which read as a fact and was a policy -- and the policy was
        # wrong. A NULL `next_run_at` already says "not due"; `paused`
        # and `retired_reason` say "will not run again", which is not
        # true of an approved errand.
        assert row["paused"] is False, (
            "the grant was retired by its own first run, so the errand "
            "cannot be run again -- this is run 345's E_NO_RUN")
        assert row["retired_reason"] is None
        assert row["run_count"] == 1
        assert row["last_execution_id"] == 4242
    _run(body)


def test_a_recurring_grant_is_still_due_again():
    """RECURRENCE IS GONE, so there is no "due again".

    This asserted a grant with runs left got a `next_run_at` and came
    back tomorrow. Every grant ever written was `@once, max_runs=1,
    run_count=1` -- sixteen of sixteen -- so the behaviour was
    reachable only from a test, while the language it justified
    ("later runs reuse that plan") was on a card people read.
    """
    pass


def test_the_ceiling_still_retires_a_recurring_grant():
    """The ceiling retired a grant that had run `max_runs` times.

    With one run there is one retirement reason -- "ran once, as
    proposed" -- and no ceiling left to reach.
    """
    pass


async def _granted(conn, **kw):
    """A goal with an approved proposal, its story, and a run on it."""
    from app.services.errand_grants import approve

    g = await _goal(conn)
    out = await approve(conn, g["goal"],
                        await _propose(conn, g["goal"], **kw), "alice")
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, "
        "state, driving_thread_id) VALUES ('errand','ops',$1,$2,'RUNNING',$3)"
        " RETURNING id", g["goal"], out["story"], out["driving_thread"])
    return g, out, eid


async def _plan_question(conn, eid, content_hash, answered=False):
    """`questions_one_open` allows ONE open question per execution, and a
    replan follows an ANSWERED plan rather than sitting beside it."""
    if answered:
        await conn.execute(
            "UPDATE kernel.questions SET status='ANSWERED', "
            "verdict='approve', answer_text='ok', answered_by='alice', "
            "answered_at=now() WHERE execution_id=$1 AND status='OPEN'", eid)
    return await conn.fetchval(
        "INSERT INTO kernel.questions (execution_id, kind, question_text, "
        "payload, status, generation, content_hash) VALUES "
        "($1,'authorization','plan?','{\"steps\":[\"a\"]}'::jsonb,'OPEN',0,$2)"
        " RETURNING id", eid, content_hash)


def test_approving_the_first_plan_pins_the_grant_to_it():
    """WITHOUT THIS THE WHOLE FEATURE IS INERT. A fresh grant has a NULL
    hash and `standing_approval` requires one, so every run would keep
    asking for acceptance and the approval a person gave would appear to
    have done nothing."""
    async def body(conn):
        from kernel.harness.park import pin_plan_to_grant, standing_approval

        g, out, eid = await _granted(conn)
        qid = await _plan_question(conn, eid, "hash-of-the-first-plan")
        assert await standing_approval(conn, eid) is None, "live too early"

        pinned = await pin_plan_to_grant(conn, qid)
        assert pinned == out["schedule"]
        grant = await standing_approval(conn, eid)
        assert grant is not None, "the grant is still not live"
        assert grant["approved_plan_hash"] == "hash-of-the-first-plan"
        assert grant["approved_by"] == "alice"
    _run(body)


def test_a_second_approval_does_not_re_pin_it():
    """THE PROPERTY I5c IS BUILT ON. If a later approval re-pinned, then
    "a re-planned errand loses its autonomy" would be false — the errand
    would rewrite its own definition of done, a person would approve the
    new plan once, and every run after that would finish unattended
    against checks nobody re-read."""
    async def body(conn):
        from kernel.harness.park import pin_plan_to_grant

        g, out, eid = await _granted(conn)
        await pin_plan_to_grant(conn, await _plan_question(conn, eid, "first"))
        again = await pin_plan_to_grant(
            conn, await _plan_question(conn, eid, "SECOND", answered=True))
        assert again is None, "a later plan re-pinned the grant"
        assert "first" == await conn.fetchval(
            "SELECT approved_plan_hash FROM kernel.errand_schedules "
            "WHERE id = $1", out["schedule"])
    _run(body)


def test_a_plan_in_another_goal_pins_nothing():
    async def body(conn):
        from kernel.harness.park import pin_plan_to_grant

        g, out, _ = await _granted(conn)
        other = await _goal(conn)
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, state) "
            "VALUES ('errand','ops',$1,'RUNNING') RETURNING id",
            other["goal"])
        assert await pin_plan_to_grant(
            conn, await _plan_question(conn, eid, "elsewhere")) is None
        assert None is await conn.fetchval(
            "SELECT approved_plan_hash FROM kernel.errand_schedules "
            "WHERE id = $1", out["schedule"])
    _run(body)


def test_a_revoked_grant_is_never_pinned():
    """Revoking is the person taking it back; a later plan approval must
    not quietly reinstate it."""
    async def body(conn):
        from kernel.harness.park import pin_plan_to_grant

        g, out, eid = await _granted(conn)
        await conn.execute(
            "UPDATE kernel.errand_schedules SET revoked_at = now() "
            "WHERE id = $1", out["schedule"])
        assert await pin_plan_to_grant(
            conn, await _plan_question(conn, eid, "h")) is None
    _run(body)


def test_the_approval_path_calls_the_pin_and_only_for_a_plan():
    """One declaration. Two callers reach an approval — the CP endpoint
    and the console — so a rule written in either is one the other does
    not have."""
    import io
    import pathlib

    src = _park_family()
    body = src[src.rindex("if settled == \"approve\":"):]
    body = body[:body.index("row = await conn.fetchrow")]
    assert "pin_plan_to_grant(conn, request_id)" in body
    # An acceptance is not a plan, and pinning on one would write the
    # acceptance question's hash as though it were the plan's.
    assert "if is_plan:" in body


def test_the_machine_is_on_the_card_when_go_asked_for_one():
    """THE LAST MOMENT ANYBODY SEES IT. An errand's story is created at
    approval and dispatched immediately, so there is no page on which to
    change the rung afterwards -- and approving a recurring `large`
    errand is a different decision from approving a `small` one."""
    from app.services.errand_proposal_cards import build_card

    body = build_card({
        "id": 9, "goal_id": "g", "proposed_by": "go", "cron": "0 9 * * 1",
        "task": "run the slow suite", "name": "", "rationale": "r",
        "max_runs": 4, "expires_at": None,
        "sandbox_size": "medium"})["body"]
    assert "`medium` machine" in body


def test_the_default_machine_is_not_announced():
    """"small (default)" on every card is noise on the common case, and
    noise is what makes people stop reading the bounds."""
    from app.services.errand_proposal_cards import build_card

    body = build_card({
        "id": 10, "goal_id": "g", "proposed_by": "go", "cron": "@once",
        "task": "t", "name": "", "rationale": "r", "max_runs": 1,
        "expires_at": None, "sandbox_size": None})["body"]
    assert "machine" not in body
