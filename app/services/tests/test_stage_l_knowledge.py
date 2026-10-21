"""Stage L's invariants — what the goal knows.

Four claims, each of which fails silently if it drifts:

  1. A plan is a BUNDLE. Cycle 2 supersedes only the decisions it names;
     an untouched decision from cycle 1 stays live. Getting this wrong
     makes a planner work around something that still stands.
  2. A decision from an APPROVED plan is `observed`; from an unapproved
     one it is `unproven`. A proposal reported as settled is the worst
     failure available here.
  3. An alignment flag cannot be raised without evidence, and cannot be
     raised twice while it is open.
  4. Knowledge ACCUMULATES per cycle. A finding at leg 5 must not erase
     one from leg 3.

1 and 2 need nothing. 3 and 4 need the local stack and SKIP without it;
every DB test runs inside a transaction that is always rolled back.
"""
from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from kernel.knowledge.alignment import fingerprint
from kernel.knowledge.decisions import address, live, normalise
from kernel.knowledge.write import _merge

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-stagel-test"


# ── 1. A plan is a bundle ────────────────────────────────────────────────

def _plan(cycle, decisions, approver="sam"):
    return {"cycle": cycle, "decisions": decisions, "approved_by": approver,
            "approved_at": None, "hash": f"h{cycle}"}


def test_a_later_plan_supersedes_only_what_it_names():
    """The correction that shaped this stage."""
    plans = [
        _plan(1, [{"id": "d1", "what": "FastAPI for the HTTP layer"},
                  {"id": "d2", "what": "stage rows in a table"}]),
        _plan(2, [{"id": "d1", "what": "parse in memory",
                   "supersedes": "cycle1:d2"}]),
    ]
    alive, dead = live(plans)
    alive_at = {d["address"] for d in alive}
    dead_at = {d["address"] for d in dead}

    # The framework choice was never mentioned in cycle 2. It stands.
    assert "cycle1:d1" in alive_at, (
        "an untouched decision from an earlier plan was reported as stale — "
        "the planner would work around something that still holds")
    assert "cycle2:d1" in alive_at
    assert dead_at == {"cycle1:d2"}
    assert next(d for d in dead)["superseded_by"] == "cycle2:d1"


def test_an_earlier_plan_cannot_retire_a_later_one():
    """Ordering is the only rule here, and it makes replay deterministic."""
    plans = [
        _plan(1, [{"id": "d1", "what": "A", "supersedes": "cycle2:d1"}]),
        _plan(2, [{"id": "d1", "what": "B"}]),
    ]
    alive, _ = live(plans)
    assert "cycle2:d1" in {d["address"] for d in alive}


def test_decisions_are_addressable_and_empty_ones_are_dropped():
    got = normalise([
        {"what": "FastAPI", "why": "async"},
        {"what": "   "},                       # nothing said — dropped
        "a bare string is a decision too",
    ])
    assert [d["what"] for d in got] == ["FastAPI",
                                        "a bare string is a decision too"]
    assert address(2, got[0]["id"]) == "cycle2:d1"


# ── 2. Provenance ────────────────────────────────────────────────────────

def test_an_unapproved_plan_is_not_evidence():
    """A proposal nobody signed must not read as a settled decision."""
    approved, _ = live([_plan(1, [{"id": "d1", "what": "X"}], approver="sam")])
    proposed, _ = live([_plan(1, [{"id": "d1", "what": "X"}], approver="")])
    assert approved[0]["provenance"] == "observed"
    assert proposed[0]["provenance"] == "unproven"


# ── 3. Alignment flags ───────────────────────────────────────────────────

def test_the_fingerprint_ignores_wording():
    """Rephrasing the same observation must not slip a duplicate past."""
    ev = [{"kind": "decision", "id": "cycle2:d1", "says": "FastAPI"}]
    a = fingerprint("story", "s1", ev)
    b = fingerprint("story", "s1",
                    [{"kind": "decision", "id": "cycle2:d1", "says": "different words"}])
    assert a == b, "a reworded flag would be raised twice"
    assert a != fingerprint("story", "s2", ev)


# ── 4. Knowledge accumulates ─────────────────────────────────────────────

def test_a_later_finding_does_not_erase_an_earlier_one():
    entries = _merge([], 1, {"learned": [{"fact": "rate limit is 5/s"}],
                             "_provenance": "unproven"}, "exec")
    entries = _merge(entries, 1, {"learned": [{"fact": "feed is UTF-8"}],
                                  "_provenance": "unproven"}, "exec")
    facts = [l["fact"] for l in entries[0]["learned"]]
    assert facts == ["rate limit is 5/s", "feed is UTF-8"], (
        "the second write replaced the first — the whole value of the "
        "record is that it survives the turn that wrote it")


def test_provenance_is_per_field_not_per_entry():
    """The evidence half is a join; the model's half is a statement."""
    entries = _merge([], 1, {"techniques": ["fastapi"],
                             "_provenance": "unproven"}, "exec")
    entries = _merge(entries, 1, {"evidence": {"landed_on": ["main"]}}, "cp")
    prov = entries[0]["provenance"]
    assert prov["techniques"] == "unproven"
    assert prov["evidence"] == "observed", (
        "a planner must be able to tell a branch that merged from a "
        "framework somebody said they used")


# ── the database half ────────────────────────────────────────────────────

async def _connect_or_skip():
    attempts = []
    for dsn in (os.environ.get("STAGEK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                             # noqa: BLE001
            attempts.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
            continue
        await conn.execute("SET search_path TO kernel, public")
        has = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            " WHERE table_name = 'alignment_flags'")
        if has:
            return conn
        await conn.close()
        attempts.append("no alignment_flags — run migrations/apply.py")
    pytest.skip("no migrated database. Tried: " + "; ".join(attempts))


@contextlib.asynccontextmanager
async def _rolled_back():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        left = await conn.fetchval(
            "SELECT count(*) FROM alignment_flags WHERE goal_id = $1",
            f"{PREFIX}-goal")
        await conn.close()
        assert left == 0, "fixture rows survived the rollback"


def test_a_flag_without_evidence_is_refused_by_the_database():
    """Not by a prompt. That is the difference between a rule and advice."""
    async def run():
        async with _rolled_back() as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "INSERT INTO alignment_flags "
                    " (goal_id, subject_kind, subject_id, question, evidence,"
                    "  fingerprint) VALUES ($1,'story','s1','why?','[]',$2)",
                    f"{PREFIX}-goal", "fp0")
    asyncio.run(run())


def test_the_same_observation_cannot_be_raised_twice():
    async def run():
        async with _rolled_back() as conn:
            args = (f"{PREFIX}-goal", "story", "s1", "is that deliberate?",
                    '[{"id":"cycle2:d1"}]', "fp1")
            sql = ("INSERT INTO alignment_flags (goal_id, subject_kind, "
                   " subject_id, question, evidence, fingerprint) "
                   " VALUES ($1,$2,$3,$4,$5::jsonb,$6)")
            await conn.execute(sql, *args)
            # A SAVEPOINT around the statement expected to fail. Postgres
            # aborts the whole transaction on a constraint violation, so
            # without this the assertions AFTER the expected failure all die
            # with InFailedSQLTransactionError — the test would report a
            # broken constraint when the constraint had just worked.
            sp = conn.transaction()
            await sp.start()
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(sql, *args)
            await sp.rollback()
            # …but it may be raised again once it has been answered.
            await conn.execute(
                "UPDATE alignment_flags SET status='RESOLVED', "
                " outcome='deliberate', resolved_at=now() WHERE goal_id=$1",
                f"{PREFIX}-goal")
            await conn.execute(sql, *args)
    asyncio.run(run())


# ── 5. Write scoping: the assistant's own name is the guard ──────────────

def test_the_actor_names_the_caller_not_the_goal():
    """The gap that made scoping impossible, and how it is detected.

    The header used to be `assistant:{goalId}` — it named the goal, which
    the URL path already carried, and not the caller. Every assistant in a
    goal sent the identical string. Detection is deterministic rather than
    heuristic: the old form put the goal id where the agent id now goes.
    """
    from kernel.knowledge.ownership import agent_of, names_an_agent

    assert agent_of("assistant:abhi-shca-ker14") == "abhi-shca-ker14"
    assert names_an_agent("assistant:abhi-shca-ker14", "KER14") is True
    # The old shape, recognised as carrying no identity.
    assert names_an_agent("assistant:KER14", "KER14") is False
    # GO is not an assistant and must never satisfy this.
    assert names_an_agent("go:KER14", "KER14") is False


def test_only_the_assistant_that_worked_a_story_may_record_against_it():
    async def run():
        async with _rolled_back() as conn:
            g = f"{PREFIX}-goal"
            await conn.execute(
                "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                g, "stage l fixture", f"{PREFIX}-user")
            for sid in (f"{PREFIX}-mine", f"{PREFIX}-theirs"):
                await conn.execute(
                    "INSERT INTO stories (id, goal_id, title, created_by) "
                    " VALUES ($1,$2,$3,$4)", sid, g, sid, f"{PREFIX}-user")
            # A thread each, on DIFFERENT assistants.
            await conn.execute(
                "INSERT INTO threads (id, goal_id, agent_id, story_id, "
                " created_by, title) VALUES ($1,$2,$3,$4,$5,$6)",
                f"{PREFIX}-t1", g, "abhi-shca", f"{PREFIX}-mine",
                f"{PREFIX}-user", "t1")
            await conn.execute(
                "INSERT INTO threads (id, goal_id, agent_id, story_id, "
                " created_by, title) VALUES ($1,$2,$3,$4,$5,$6)",
                f"{PREFIX}-t2", g, "sam-shca", f"{PREFIX}-theirs",
                f"{PREFIX}-user", "t2")

            from kernel.knowledge.ownership import may_write
            # The gateway id carries the goal suffix; the thread holds the
            # bare agent id. Exact-or-prefix, case-insensitively.
            actor = "assistant:abhi-shca-KER14"
            ok, _ = await may_write(conn, g, actor, f"{PREFIX}-mine")
            assert ok is True

            ok, why = await may_write(conn, g, actor, f"{PREFIX}-theirs")
            assert ok is False
            assert "have not worked it" in why, why
            # The refusal must point at what IS allowed, or it teaches
            # nothing and the model retries.
            assert "goal_knowledge" in why

            ok, why = await may_write(conn, g, actor, "no-such-story")
            assert ok is False and "not in this goal" in why

            # An UNIDENTIFIED caller (old gateway) falls back to goal
            # scope rather than wedging a tool on a deploy ordering.
            ok, _ = await may_write(conn, g, f"assistant:{g}",
                                    f"{PREFIX}-theirs")
            assert ok is True
    asyncio.run(run())


# ── 6. The stored kind, which was wrong in two ways at once ──────────────

def test_the_plan_kind_matches_what_park_actually_stores():
    """`plan_approval` is the CALLER's word; `authorization` is stored.

    The first version of `plans_for` queried `human_requests` for kind
    `plan_approval` — a table that does not exist in this database and a
    kind that is never written. Both names came off the E6h migration
    instead of off the code, and the surface failed on its first call with
    `relation "human_requests" does not exist`.

    park.py owns the mapping. This asserts the reader agrees with it, so
    the two cannot part again without something going red.
    """
    from kernel.harness.park import _KIND
    from kernel.knowledge.read import PLAN_KIND

    assert _KIND["plan_approval"] == PLAN_KIND, (
        f"park stores plan approvals as {_KIND['plan_approval']!r} and the "
        f"knowledge reader looks for {PLAN_KIND!r}")


def test_only_an_approved_plan_counts_as_approved():
    """ANSWERED is not APPROVED, and the gap is where citations go wrong.

    Mirrors park.plan_is_approved exactly: declined is answered and is not
    permission; `revise` is answered and is not permission either — the
    person asked for a different plan.
    """
    async def run():
        async with _rolled_back() as conn:
            g = f"{PREFIX}-goal"
            sid = f"{PREFIX}-plans"
            await conn.execute(
                "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                g, "stage l plans", f"{PREFIX}-user")
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by) "
                " VALUES ($1,$2,$3,$4)", sid, g, "plans", f"{PREFIX}-user")
            eid = await conn.fetchval(
                "INSERT INTO executions (kind, domain, goal_id, story_id, "
                " state, task) VALUES ('story','coding',$1,$2,'RUNNING','t') "
                " RETURNING id", g, sid)

            async def park(payload, status, declined, verdict, who):
                # `answer_text` is NOT optional on an ANSWERED row —
                # questions_answer_present refuses it, the same way the
                # kernel refuses a verdict with nothing in it.
                import json
                await conn.execute(
                    "INSERT INTO questions (execution_id, kind, question_text,"
                    " payload, content_hash, status, declined, verdict,"
                    " answer_text, answered_by, generation) VALUES "
                    " ($1,'authorization','plan?',$2::jsonb,'h',$3,$4,$5,$6,$7,1)",
                    eid, json.dumps(payload), status, declined, verdict,
                    ("ok" if status == "ANSWERED" else None), who)

            await park({"decisions": [{"id": "d1", "what": "FastAPI"}]},
                       "ANSWERED", False, "approve", "sam")
            await park({"decisions": [{"id": "d1", "what": "Flask"}]},
                       "ANSWERED", True, None, "sam")          # declined
            await park({"decisions": [{"id": "d1", "what": "Django"}]},
                       "ANSWERED", False, "revise", "sam")     # send-back
            await park({"decisions": [{"id": "d1", "what": "Bottle"}]},
                       "OPEN", False, None, None)              # still open

            from kernel.knowledge.read import plans_for
            plans = await plans_for(conn, sid)
            assert [p["cycle"] for p in plans] == [1, 2, 3, 4]
            assert [bool(p["approved_by"]) for p in plans] == \
                [True, False, False, False], \
                "a declined, revised or still-open plan was reported approved"

            from kernel.knowledge.decisions import live
            alive, _ = live(plans)
            provs = {d["address"]: d["provenance"] for d in alive}
            assert provs["cycle1:d1"] == "observed"
            # The rest are proposals. Citing one as settled would be citing
            # something nobody agreed to.
            assert provs["cycle2:d1"] == "unproven"
            assert provs["cycle3:d1"] == "unproven"
            assert provs["cycle4:d1"] == "unproven"
    asyncio.run(run())
