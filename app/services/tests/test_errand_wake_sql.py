"""I5g · GO learns an errand parked, without polling for it.

"THE GO SHOULD BE WATCHING OVER THE ERRAND" was, before this, a poll GO
had no reason to run. A park writes a question and posts a card to
whoever is driving; nothing reached the planner. So an errand parked on
a clarification GO could answer in one sentence sat waiting for a person
to notice a thread they were not watching.

ERRANDS ONLY, and that boundary is a requirement rather than a
simplification: a story's park already has an assistant whose entire job
is that story, and waking GO there would put two agents on one question
and change how story executions behave.

THE WAKE'S FIRST JOB IS TO SAY WHETHER GO CAN ACT. GO holds
`relay_answer`, and four of the five parking kinds refuse it. A wake
that only said "an errand parked" would have GO try the tool and read
§1.3 as a fault.

NOTIFY plus a poll backstop, like every other channel here — a lost
notification is a late wake, never a missing one.
"""
from __future__ import annotations

import pathlib
import asyncio
import json
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-wake-test"


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
                "WHERE table_schema='kernel' AND table_name='questions'"):
            return conn
        await conn.close()
    pytest.skip("no local database with the kernel schema")



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


async def _run_of(conn, kind="errand"):
    tag = os.urandom(4).hex()
    goal, story = f"{PREFIX}-g-{tag}", f"{PREFIX}-s-{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')",
        goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'s','accepted','alice')", story, goal)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, "
        "state) VALUES ($3,$4,$1,$2,'RUNNING') RETURNING id",
        goal, story, kind, "ops" if kind == "errand" else "coding")
    return {"goal": goal, "story": story, "eid": eid}


# ── the NOTIFY fires for an errand and only for an errand ────────────────

class _Spy:
    """A connection that records what it was asked to execute.

    A REAL LISTENER CANNOT SEE THIS. Postgres delivers NOTIFY on COMMIT,
    and every test here rolls back — so listening would prove nothing
    either way. Recording the statement proves the two things that
    actually matter: whether one was sent, and what was in it.
    """

    def __init__(self, conn):
        self._conn = conn
        self.notified = []

    async def fetchrow(self, *a, **kw):
        return await self._conn.fetchrow(*a, **kw)

    async def execute(self, sql, *args, **kw):
        if "pg_notify" in sql:
            self.notified.append((sql, args))
            return "NOTIFY"
        return await self._conn.execute(sql, *args, **kw)


async def _park_and_spy(conn, kind, question_kind, park_kind):
    from kernel.harness.park import _notify_parked

    f = await _run_of(conn, kind=kind)
    qid = await conn.fetchval(
        "INSERT INTO kernel.questions (execution_id, kind, question_text, "
        "payload, status, generation, content_hash) "
        "VALUES ($1,$3,'q?','{}'::jsonb,'OPEN',0,$2) RETURNING id",
        f["eid"], f"k-{f['eid']}", question_kind)
    spy = _Spy(conn)
    await _notify_parked(spy, f["eid"], qid, park_kind)
    return f, qid, spy


def test_a_parked_errand_notifies_go_with_what_it_needs():
    """The channel, and every field the dispatch branch reads."""
    async def body(conn):
        f, qid, spy = await _park_and_spy(
            conn, "errand", "clarification", "question")
        assert len(spy.notified) == 1, "no NOTIFY for a parked errand"
        sql, args = spy.notified[0]
        assert "go_execution_parked" in sql
        payload = json.loads(args[0])
        assert payload == {"goal": f["goal"], "execution": f["eid"],
                           "question": qid, "kind": "clarification"}
    _run(body)


def test_the_wire_kind_is_the_question_kind_not_the_park_word():
    """`open_request` speaks `plan_approval` and the questions table
    stores `authorization`. Sending the caller's word would have the
    handler comparing it to a kind that never appears in the table, so
    every approval park would read as settleable by GO."""
    async def body(conn):
        _, _, spy = await _park_and_spy(
            conn, "errand", "authorization", "plan_approval")
        assert json.loads(spy.notified[0][1][0])["kind"] == "authorization"
    _run(body)


def test_a_parked_story_notifies_nobody():
    """The constraint the whole stage works under: story executions are
    not to be affected. A story's park has an assistant on it already,
    and a second agent on one question is a change in how stories run."""
    async def body(conn):
        _, _, spy = await _park_and_spy(
            conn, "story", "authorization", "plan_approval")
        assert not spy.notified, "a story park woke GO"
    _run(body)


def test_a_run_that_vanished_notifies_nobody():
    """The row is read before the payload is built, so a run deleted
    between the park and here must not become a KeyError inside the
    best-effort try — which would be logged as a transient failure for
    ever."""
    async def body(conn):
        from kernel.harness.park import _notify_parked

        spy = _Spy(conn)
        await _notify_parked(spy, -1, -1, "question")
        assert not spy.notified
    _run(body)


def test_the_notify_is_best_effort_and_never_stops_a_park():
    """A run that could not ask because a NOTIFY failed would be a
    notification bug costing a capability. Same reasoning as the
    batching call it sits beside."""
    import io
    import pathlib

    src = _park_family()
    body = src[src.index("async def open_request"):]
    body = body[:body.index("async def _notify_parked")]
    call = body[body.index("_notify_parked("):]
    assert "except Exception" in body[body.index("_notify_parked("):], (
        "an unhandled NOTIFY failure stops the run from asking")
    # And AFTER the park, so a failure cannot leave the run un-parked.
    assert body.index("state = 'PARKED_HOT'") < body.index("_notify_parked(")
    assert call


# ── the handler: what GO is told ─────────────────────────────────────────

def _wake(kind, text="which column did you mean?", mine=False):
    from app.services.go_wake_events import _parked_wake

    return _parked_wake({"execution": 12, "question": 34, "kind": kind,
                         "question_text": text, "proposed_by_me": mine})


def test_a_clarification_tells_go_to_answer_it():
    body = _wake("clarification")
    # THE HEADER NOW SAYS WHO MAY ACT, which is the fact that decides
    # everything else; the run and question ids moved into the
    # imperative, where they are used.
    assert body.startswith("[ERRAND NEEDS YOUR ANSWER]")
    assert "question_id=34" in body
    assert "which column did you mean?" in body
    assert body.startswith("[ERRAND NEEDS YOUR ANSWER]")
    assert "relay_answer(question_id=34" in body
    # AND WHAT TO DO WHEN IT DOES NOT KNOW. A wake that only says
    # "answer this" gets an answer whether or not GO has one, and a
    # wrong answer here becomes the work.
    assert "do not guess" in body


@pytest.mark.parametrize("kind", ["authorization", "acceptance",
                                  "permission", "escalation"])
def test_an_approval_tells_go_it_cannot_and_what_is_useful_instead(kind):
    """A wake that said "an errand parked, deal with it" would have GO
    calling `relay_answer`, reading E_CANNOT_APPROVE, and treating §1.3
    as a fault to route around."""
    body = _wake(kind, text="may I run terraform destroy?")
    assert "cannot settle it" in body
    assert "relay_answer` will refuse you" in body
    assert "already in their thread" in body, (
        "GO is not told a person has been asked, so it may ask again")
    assert "report back to" in body, "GO is told to do nothing at all"
    assert "relay_answer(question_id" not in body, (
        "the wake tells GO to call a tool that will refuse it")


def test_an_errand_go_proposed_says_answer_but_say_so():
    """I5g CORRECTED I5e HERE. The prompt first said GO must never answer
    a question on an errand it proposed — stricter than the design asks
    and stricter than the mechanism needs, since a clarification cannot
    approve, accept or permit anything and the kinds that can already
    refuse GO by privilege. Forbidding it would have made "GO answers and
    watches over the errand" impossible on the very errands GO watches.

    What the risk names is silence, not answering: the person who
    approved the errand owns every run of it."""
    body = _wake("clarification", mine=True)
    assert "You proposed this errand" in body
    assert "say in your report that you settled this yourself" in body
    assert body.startswith("[ERRAND NEEDS YOUR ANSWER]"), (
        "GO was told to sit on its hands")


def test_a_park_on_somebody_elses_errand_says_nothing_about_proposing():
    assert "You proposed" not in _wake("clarification", mine=False)


# ── the handler refuses what it should ────────────────────────────────────
#
# EVERY ONE OF THESE ASSERTS `_deliver` WAS NOT REACHED, not merely that
# the handler returned False. `_deliver` returns False on its own when a
# goal has no GO thread — which is true of every goal these fixtures
# make — so a plain `is False` passes with the guard removed. It did:
# the first version of the story test stayed green through a mutation
# that deleted the check it was written for.


def _watch_deliver(monkeypatch):
    import app.services.go_wake_events as ev

    calls = []

    # `thread_id` IS RECORDED, not just tolerated. An errand's park wake
    # goes to the errand's OWN thread -- every park used to land in the
    # goal's main conversation, the one a person reads and the one whose
    # context window has to last the goal. A double that swallowed the
    # argument would let that regress silently, which is how this
    # started: the double predated the parameter and the suite went red
    # in CI rather than here.
    async def fake(pool, goal, kind, ref, content, thread_id=""):
        calls.append({"goal": goal, "kind": kind, "ref": ref,
                      "content": content, "thread_id": thread_id})
        return True

    monkeypatch.setattr(ev, "_deliver", fake)
    return calls


def test_an_open_errand_park_does_reach_go(monkeypatch):
    """The happy path, so the refusals below mean something. Without it
    a handler that refused everything would satisfy all four."""
    calls = _watch_deliver(monkeypatch)

    async def body(conn):
        import app.services.go_wake_events as ev

        f = await _run_of(conn)
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'clarification','which column?','{}'::jsonb,'OPEN',"
            "0,$2) RETURNING id", f["eid"], f"ok-{f['eid']}")
        assert await ev.on_execution_parked(
            conn, f["goal"], f["eid"], qid, "clarification") is True
        assert len(calls) == 1
        assert calls[0]["kind"] == "errand_parked"
        assert calls[0]["ref"] == f"q:{qid}"
        assert "which column?" in calls[0]["content"]
    _run(body)


def test_an_answered_question_wakes_nobody(monkeypatch):
    """The normal backstop case. A wake about a settled question is noise
    that teaches GO to stop reading these."""
    calls = _watch_deliver(monkeypatch)

    async def body(conn):
        import app.services.go_wake_events as ev

        f = await _run_of(conn)
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash, "
            "verdict, answer_text, answered_by, answered_at) VALUES "
            "($1,'clarification','q','{}'::jsonb,'ANSWERED',0,$2,'revise',"
            "'that one','alice',now()) RETURNING id",
            f["eid"], f"a-{f['eid']}")
        assert await ev.on_execution_parked(
            conn, f["goal"], f["eid"], qid, "clarification") is False
        assert not calls
    _run(body)


def test_a_story_park_is_refused_by_the_handler_too(monkeypatch):
    """Belt and braces: the NOTIFY filters, the backstop query filters,
    and so does this. A story park reaching GO would put two agents on
    a question that already has an assistant."""
    calls = _watch_deliver(monkeypatch)

    async def body(conn):
        import app.services.go_wake_events as ev

        f = await _run_of(conn, kind="story")
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'clarification','q','{}'::jsonb,'OPEN',0,$2) "
            "RETURNING id", f["eid"], f"s2-{f['eid']}")
        assert await ev.on_execution_parked(
            conn, f["goal"], f["eid"], qid, "clarification") is False
        assert not calls, "GO was woken about a story"
    _run(body)


def test_a_question_in_another_goal_is_refused(monkeypatch):
    calls = _watch_deliver(monkeypatch)

    async def body(conn):
        import app.services.go_wake_events as ev

        mine, theirs = await _run_of(conn), await _run_of(conn)
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'clarification','q','{}'::jsonb,'OPEN',0,$2) "
            "RETURNING id", theirs["eid"], f"x-{theirs['eid']}")
        assert await ev.on_execution_parked(
            conn, mine["goal"], theirs["eid"], qid, "clarification") is False
        assert not calls
    _run(body)


# ── the channel is wired in all three places ─────────────────────────────

def test_the_channel_is_listened_dispatched_and_swept():
    """The N-declarations shape again: LISTEN without a dispatch branch
    is a notification that arrives and does nothing, a branch without the
    LISTEN never fires, and neither without the backstop means a restart
    during a park loses it for good."""
    import io
    import pathlib

    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "app/services/go_wakes.py", encoding="utf-8").read()
    assert '"go_execution_parked"' in src[:src.index("async def _dispatch")]
    body = src[src.index("async def _dispatch"):src.index("async def _backstop")]
    assert 'channel == "go_execution_parked"' in body
    assert "on_execution_parked(" in body

    sweep = src[src.index("async def _backstop"):]
    sweep = sweep[:sweep.index("async def run_forever")]
    assert "on_execution_parked(" in sweep, "a missed NOTIFY is lost"
    assert "errand_parked" in sweep, "the sweep does not dedupe on the log"
    assert "q.status = 'OPEN'" in sweep
    assert "e.kind = 'errand'" in sweep


def test_the_payload_the_notify_sends_is_the_one_dispatch_reads():
    """The two are written in different files and nothing joins them —
    a renamed key is a KeyError inside a listener, logged and swallowed,
    with GO simply never waking."""
    import io
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    park = _park_family()
    sent = park[park.index("async def _notify_parked"):]
    sent = sent[:sent.index("json.dumps(") + 400]
    keys = set(re.findall(r'"(\w+)":', sent))

    wakes = io.open(root / "app/services/go_wakes.py", encoding="utf-8").read()
    branch = wakes[wakes.index('channel == "go_execution_parked"'):]
    branch = branch[:branch.index("elif")]
    read = set(re.findall(r'data\["(\w+)"\]', branch))
    assert read, "the branch reads no payload — test is stale"
    assert read <= keys, f"dispatch reads {sorted(read - keys)}, never sent"


def test_the_wake_is_deduped_on_the_question_not_the_run():
    """An errand parks many times over its life and each park is a
    distinct thing to know about; a replayed NOTIFY for one is not."""
    import io
    import pathlib

    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "app/services/go_wake_events.py", encoding="utf-8").read()
    body = src[src.index("async def on_execution_parked"):]
    assert 'f"q:{question}"' in body
    assert "errand_parked" in body


def test_json_is_imported_where_the_payload_is_built():
    """A NameError inside the best-effort try would be caught, logged as
    "GO not notified", and look exactly like a transient failure for
    ever."""
    import io
    import pathlib

    src = _park_family()
    assert "\nimport json" in src
    assert json  # the import above is what the module needs too


# ── I5h · a cap on how many questions GO settles per run ─────────────────
#
# The design's own risk section names this: "GO answering its own
# errand's questions in a loop", bounded by nothing that exists. Three
# attempts before a clarification is allowed, one open question per
# execution, content-hash dedupe and max_legs all bound how often the
# SAME question is asked; none bounds how many DIFFERENT ones one agent
# answers for another, which is the shape a two-model loop takes.


async def _answered(conn, eid, n, actor="go"):
    """`n` answers already relayed by `actor` on this run."""
    for i in range(n):
        await conn.execute(
            "INSERT INTO kernel.proposals (execution_id, actor_id, kind, "
            "payload, idempotency_key) VALUES "
            "($1,$2,'answer','{}'::jsonb,$3)",
            eid, actor, f"k-{eid}-{actor}-{i}-{os.urandom(3).hex()}")


async def _open_question(conn, eid, kind="clarification"):
    return await conn.fetchval(
        "INSERT INTO kernel.questions (execution_id, kind, question_text, "
        "payload, status, generation, content_hash) VALUES "
        "($1,$3,'which one?','{}'::jsonb,'OPEN',0,$2) RETURNING id",
        eid, f"q-{eid}-{os.urandom(3).hex()}", kind)


# AGAINST `CAP`, NOT AGAINST A LITERAL. The number moved once already --
# 3 to 10, when GO went from planning an errand and leaving to holding a
# thread and watching it -- and a parametrize list full of 3s went red
# for a deliberate change while testing nothing about the property. What
# matters is that GO answers up to the cap, escalates past it, and that
# the escalated question still reaches somebody.
from kernel.go.answers import CAP as _CAP

@pytest.mark.parametrize("already,capped", [(0, False), (1, False),
                                            (_CAP - 1, False),
                                            (_CAP, True),
                                            (_CAP + 6, True)])
def test_go_answers_up_to_the_cap_and_then_must_escalate(already, capped):
    async def body(conn):
        from kernel.go.answers import may_answer

        f = await _run_of(conn)
        await _answered(conn, f["eid"], already)
        got = await may_answer(conn, await _open_question(conn, f["eid"]),
                               "go")
        if capped:
            assert got is not None and got.code == "E_ANSWER_CAP"
        else:
            assert got is None, f"refused at {already} answers"
    _run(body)


def test_the_cap_is_per_run_and_not_per_goal():
    """A goal with twenty healthy errands must not run out of answers
    because one of them was chatty."""
    async def body(conn):
        from kernel.go.answers import may_answer

        busy = await _run_of(conn)
        await _answered(conn, busy["eid"], 5)

        # A SECOND STORY, not a second run of the same one. This used to
        # reuse `busy["story"]` as a convenience, and
        # `executions_one_live_per_story` now refuses it -- rightly: two
        # live executions on one story is the duplicate-dispatch bug,
        # not the "other errand in the same goal" this test is about.
        other_story = busy["story"] + "-b"
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by) "
            "VALUES ($1,$2,'s2','accepted','alice')",
            other_story, busy["goal"])
        other = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, story_id,"
            " state) VALUES ('errand','ops',$1,$2,'RUNNING') RETURNING id",
            busy["goal"], other_story)
        assert await may_answer(
            conn, await _open_question(conn, other), "go") is None
    _run(body)


def test_the_cap_counts_this_actor_and_not_every_agent():
    """Two orchestrators in one goal is not a thing today, and a cap that
    pooled their counts would silently become half a cap each the day it
    is — which is the sort of thing that gets discovered in production."""
    async def body(conn):
        from kernel.go.answers import may_answer

        f = await _run_of(conn)
        await _answered(conn, f["eid"], 3, actor="some-other-agent")
        assert await may_answer(
            conn, await _open_question(conn, f["eid"]), "go") is None
    _run(body)


def test_the_cap_counts_answers_and_not_every_proposal():
    """A steer, a cancel and a replan are not answers. Counting every
    proposal would have GO capped by things it did not do."""
    async def body(conn):
        from kernel.go.answers import may_answer

        f = await _run_of(conn)
        for i, kind in enumerate(("steer", "cancel", "replan")):
            await conn.execute(
                "INSERT INTO kernel.proposals (execution_id, actor_id, kind,"
                " payload, idempotency_key) VALUES "
                "($1,'go',$2,'{}'::jsonb,$3)",
                f["eid"], kind, f"p-{f['eid']}-{i}")
        assert await may_answer(
            conn, await _open_question(conn, f["eid"]), "go") is None
    _run(body)


def test_a_capped_run_is_parked_with_a_person_and_not_stranded():
    """THE REASON THE CAP IS SAFE. It does not send anybody a new
    interruption — the card poller posts one for EVERY open question
    whatever its kind, so the person driving already has this one. The
    cap stops GO answering and leaves the card they already had."""
    async def body(conn):
        from app.services.kernel_park_cards import _pending
        from kernel.go.answers import may_answer

        f = await _run_of(conn)
        # The driving thread, as an autonomous errand would have.
        tid = f"{PREFIX}-th-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO threads (id, goal_id, created_by, title, story_id) "
            "VALUES ($1,$2,'alice','a',$3)", tid, f["goal"], f["story"])
        await conn.execute(
            "UPDATE kernel.executions SET driving_thread_id = $2 "
            "WHERE id = $1", f["eid"], tid)
        await _answered(conn, f["eid"], _CAP)
        qid = await _open_question(conn, f["eid"])

        assert (await may_answer(conn, qid, "go")).code == "E_ANSWER_CAP"
        rows = [r for r in await _pending(conn) if r["id"] == qid]
        assert rows, "the capped question reaches nobody — the run is stuck"
        assert rows[0]["thread_id"] == tid
        # And the run is still parked, not failed.
        assert "RUNNING" == await conn.fetchval(
            "SELECT state FROM kernel.executions WHERE id = $1", f["eid"])
    _run(body)


def test_the_refusal_tells_go_what_to_do_instead():
    """A refusal that only says no gets retried with different wording."""
    async def body(conn):
        from kernel.go.answers import may_answer

        f = await _run_of(conn)
        await _answered(conn, f["eid"], _CAP)
        msg = (await may_answer(
            conn, await _open_question(conn, f["eid"]), "go")).message
        assert "not lost" in msg.lower()
        assert "goal thread" in msg
        assert "task text" in msg
    _run(body)


def test_the_cap_is_on_gos_branch_and_not_inside_relay_answer():
    """THE STORY PATH, which this stage may not change. The assistant
    relays a PERSON's answer through the same function — a cap inside it
    would ration how often somebody may answer their own story's
    questions."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    write = io.open(root / "kernel/assistant/write.py", encoding="utf-8").read()
    body = write[write.index("async def relay_answer"):]
    body = body[:body.index("async def cancel_story")]
    assert "may_answer" not in body and "E_ANSWER_CAP" not in body, (
        "the cap is inside relay_answer, so it applies to a person's "
        "relayed answer too")

    api = io.open(root / "kernel/assistant/api.py", encoding="utf-8").read()
    go = api[api.index("async def _dispatch_go"):]
    assert "may_answer(" in go, "GO's branch does not apply the cap"
    # And BEFORE the call it guards, not after.
    assert go.index("may_answer(") < go.index("await relay_answer(")


def test_the_prompt_tells_go_the_cap_exists():
    """A limit an agent meets only by hitting it reads as a malfunction.

    WITHOUT NAMING THE NUMBER. The prompt used to say "three answers per
    run" while the code said 3, and when the cap moved to 10 the prose
    was silently wrong -- a number maintained in two places drifts, and
    the prompt is the copy nobody recompiles. What the model needs is
    that a cap EXISTS, that hitting it is the design rather than a
    fault, and what to do then."""
    import io
    import pathlib

    text = io.open(pathlib.Path(__file__).resolve().parents[3]
                   / "kernel/go/AGENTS.md", encoding="utf-8").read().lower()
    assert "there is a cap" in text
    assert "the design working" in text
    for word in ("three", "four", "five", "ten"):
        assert f"{word} answers per run" not in text, (
            f"the cap is named as {word!r} in prose; it lives in "
            f"kernel/go/answers.py and nowhere else")
    assert "task text was wrong" in text
