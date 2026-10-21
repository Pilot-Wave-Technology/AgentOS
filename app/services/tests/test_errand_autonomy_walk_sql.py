"""Phase 5 · the whole autonomy path, walked once from end to end.

EVERY STAGE OF I5 IS TESTED IN ISOLATION AND THAT IS NOT THE SAME THING.
Each of those files mocks or hand-builds what the stage before it should
have produced, so a seam where two stages disagree — a column one writes
and the other never reads, a state one leaves that the next does not
expect — passes both. This walks it: GO proposes, a person approves, the
sweep creates a run, the plan is approved, the pin lands, the self-check
passes, and the errand finishes on its own.

WHAT "ON ITS OWN" MEANS HERE, and the thing worth reading the walk for:
a person approved TWICE — the errand, then its plan — before anything
happened unattended, and their uid is on every step of it. Autonomy is
the SECOND approval moving earlier, not disappearing.

Real Postgres, one transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-walk-test"


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


def test_the_whole_path_from_gos_ask_to_an_unattended_finish():
    async def body(conn):
        from app.services.errand_grants import approve
        from app.services.errand_proposal_cards import build_card
        from kernel.go.errands import propose_errand
        from kernel.harness.park import (pin_plan_to_grant,
                                         standing_approval)
        from kernel.ledger.actors import principal_for
        from kernel.worker.schedules import sweep

        tag = os.urandom(4).hex()
        goal = f"{PREFIX}-{tag}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')",
            goal)
        # TWO PEOPLE, and bob's thread is newer. Every "whose run is this"
        # question below has a wrong answer available, so the right one
        # is a fact rather than the only option.
        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        threads = {}
        for offset, who in enumerate(("alice", "bob")):
            tid = f"{PREFIX}-th-{who}-{tag}"
            await conn.execute(
                # `agent_id` because every real thread has one -- 231 of
                # 231 in the live database -- and the approval path reads
                # it to decide WHICH assistant instance the errand's own
                # thread belongs to. A fixture without it tests a shape
                # that does not occur.
                "INSERT INTO threads (id, goal_id, agent_id, created_by, "
                "title, created_at) VALUES ($1,$2,$3,$4,$5,$6)",
                tid, goal, f"assistant-{who}", who, f"{who}'s",
                base + dt.timedelta(minutes=offset))
            threads[who] = tid

        # ── 1 · GO asks. Nothing runs. ────────────────────────────────
        asked = await propose_errand(
            conn, goal,
            task="read yesterday's error log and report the top five",
            rationale="the rate doubled on Tuesday and nobody reads it "
                      "daily",
            # REQUIRED now: an autonomous errand's story is created at
            # approval, so there is no second call to grant in. This walk
            # is about the autonomy path, not about grants, so it says
            # "needs nothing" explicitly -- which is a real answer and
            # distinct from omitting it.
            resources=[],
            name="daily error digest", cron="0 9 * * *", max_runs=5)
        assert "proposal" in asked, asked
        assert 0 == await conn.fetchval(
            "SELECT count(*) FROM kernel.errand_schedules WHERE goal_id=$1",
            goal), "proposing created a schedule"

        # ── 2 · the card says what is being agreed to ─────────────────
        row = await conn.fetchrow(
            # THE SAME COLUMNS `_pending` SELECTS. This list is written out
            # by hand, so a column the card renderer starts reading has to
            # be added here too -- `sandbox_size` was, and this went red
            # with a KeyError rather than a missing sentence.
            "SELECT id, goal_id, proposed_by, task, name, rationale, cron, "
            "max_runs, expires_at, sandbox_size "
            "FROM kernel.errand_proposals WHERE id=$1",
            asked["proposal"])
        card = build_card(row)
        assert card["needs_human"] is True
        assert "You own it" in card["body"]
        # THE BOUNDS LANGUAGE WENT WITH RECURRENCE. An errand runs
        # once, so "at most N runs" described a grant shape that has
        # never existed: sixteen of sixteen are `@once, max_runs=1`.
        assert "runs once" in card["body"]

        # ── 3 · ALICE approves, though BOB's thread is newer ──────────
        granted = await approve(conn, goal, asked["proposal"], "alice")
        # ALICE'S, AND THE ERRAND'S OWN — two claims, and the test used
        # to make only the first by asserting alice's existing thread
        # directly. That is the bug this path had: a thread is
        # story-scoped, so driving the run with whichever one the
        # approver was in posts the errand's cards into another story's
        # conversation. Approval now opens a thread for this story,
        # owned by alice; `driving_thread_id` decides WHOSE, never which
        # story's.
        drove = await conn.fetchrow(
            "SELECT story_id, created_by FROM threads WHERE id = $1",
            granted["driving_thread"])
        assert drove is not None, "approval drove a thread that does not exist"
        assert drove["story_id"] == granted["story"]
        assert drove["created_by"] == "alice", (
            "the errand followed whoever posted last, not its approver")
        assert granted["driving_thread"] != threads["bob"]

        # DUE IMMEDIATELY, because an errand runs once and approval IS
        # the start. This used to assert `due > now` -- the next 09:00 of
        # a recurring schedule -- which is now false by microseconds:
        # there is no schedule, so the first and only run is due at once.
        #
        # THE PROPERTY WORTH KEEPING is that a time was set at all. An
        # approval that set none is the failure that looks exactly like
        # success: the person clicks, the row says APPROVED, and nothing
        # ever runs. Asserted BEFORE the override below, or the override
        # hides it.
        due = await conn.fetchval(
            "SELECT next_run_at FROM kernel.errand_schedules WHERE id = $1",
            granted["schedule"])
        assert due is not None, "approved, and never due"
        assert due <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=5), "an errand should be due on approval, not later"
        # WAS: the 09:00 the cron named. There is no cron to name one --
        # an errand runs once, when it is approved -- so the wall-clock
        # assertion went with recurrence. The check above, that it is
        # due NOW rather than never, is the property that mattered.

        # Now bring it forward so the sweep below has something to claim.
        await conn.execute(
            "UPDATE kernel.errand_schedules SET next_run_at = now() - "
            "interval '1 minute' WHERE id = $1", granted["schedule"])

        # ── 4 · the sweep creates the run, and it belongs to alice ────
        did = [a for a in await sweep(conn)
               if a["schedule"] == granted["schedule"]]
        assert did and did[0]["action"] == "scheduled", did
        eid = did[0]["execution_id"]
        run = await conn.fetchrow(
            "SELECT kind, state, goal_id, story_id, driving_thread_id "
            "FROM kernel.executions WHERE id = $1", eid)
        assert run["kind"] == "errand"
        assert run["state"] == "PENDING", "the sweep dispatched something"
        assert run["story_id"] == granted["story"], (
            "no story — the board, the ticker, pause, resume, cancel and "
            "the workspace panel all key on one")
        # A THREAD OF ITS OWN, owned by alice — not alice's existing one.
        #
        # This used to assert the captured thread directly, and that is
        # the bug: a thread is STORY-scoped, so driving the run with
        # whichever one the approver happened to be in posts its cards
        # into another story's conversation. On KER25 that put a
        # plan-approval card in a finished errand's thread, where it was
        # correct and invisible.
        #
        # `driving_thread_id` decides WHOSE, not WHICH story's.
        drove = await conn.fetchrow(
            "SELECT id, story_id, created_by FROM threads WHERE id = $1",
            run["driving_thread_id"])
        assert drove is not None, "the run drives a thread that does not exist"
        assert drove["story_id"] == granted["story"], (
            "the run drives a thread belonging to a different story")
        assert drove["created_by"] == "alice", (
            "the thread is not owned by the person who approved it")
        who = await principal_for(conn, eid)
        assert who["human_uid"] == "alice", (
            "the ledger principal is not the person who approved it")

        # ── 5 · the FIRST run is still asked. This is the point. ──────
        assert await standing_approval(conn, eid) is None, (
            "the first run inherited autonomy from an unpinned grant")
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'authorization','plan?',"
            "'{\"steps\":[\"read\",\"summarise\"]}'::jsonb,'OPEN',0,$2) "
            "RETURNING id", eid, f"plan-{eid}")

        # ── 6 · alice approves the plan; the grant pins to it ─────────
        assert await pin_plan_to_grant(conn, qid) == granted["schedule"]
        grant = await standing_approval(conn, eid)
        assert grant is not None, "approving the plan did not arm the grant"
        assert grant["approved_by"] == "alice"

        # ── 7 · a later run of the SAME plan finishes unattended ──────
        await conn.execute(
            "UPDATE kernel.questions SET status='ANSWERED', "
            "verdict='approve', answer_text='ok', answered_by='alice', "
            "answered_at=now() WHERE id=$1", qid)
        await conn.execute(
            "UPDATE kernel.executions SET state='RUNNING' WHERE id=$1", eid)
        covered = await standing_approval(conn, eid)
        assert covered is not None
        assert covered["approved_plan_hash"] == f"plan-{eid}"

        # ── 8 · and a REPLAN loses it ─────────────────────────────────
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'authorization','new plan?','{}'::jsonb,'OPEN',1,$2)",
            eid, f"plan-{eid}-v2")
        assert await standing_approval(conn, eid) is None, (
            "a re-planned errand kept autonomy it was never granted for")
    _run(body)


def test_a_declined_proposal_leaves_the_goal_exactly_as_it_was():
    """The other end of the walk. A no must cost nothing and leave
    nothing half-made — no story on the board, no schedule that could
    fire later."""
    async def body(conn):
        from app.services.errand_grants import decline
        from kernel.go.errands import propose_errand

        goal = f"{PREFIX}-no-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','a')",
            goal)
        asked = await propose_errand(conn, goal, task="t",
                                     rationale="because", resources=[])
        await decline(conn, goal, asked["proposal"], "bob", "we page on it")

        assert 0 == await conn.fetchval(
            "SELECT count(*) FROM kernel.errand_schedules WHERE goal_id=$1",
            goal)
        assert 0 == await conn.fetchval(
            "SELECT count(*) FROM stories WHERE goal_id = $1", goal)
    _run(body)


def test_a_revoked_grant_stops_covering_runs_immediately():
    """`standing_approval` is read fresh per leg for this reason: a
    person changing their mind between legs has to take effect on the
    next one, not at the end of the run."""
    async def body(conn):
        from app.services.errand_grants import approve
        from kernel.go.errands import propose_errand
        from kernel.harness.park import (pin_plan_to_grant,
                                         standing_approval)

        goal = f"{PREFIX}-rev-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','a')",
            goal)
        asked = await propose_errand(conn, goal, task="t", rationale="r",
                                     resources=[])
        granted = await approve(conn, goal, asked["proposal"], "alice")
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, story_id,"
            " state) VALUES ('errand','ops',$1,$2,'RUNNING') RETURNING id",
            goal, granted["story"])
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, kind, "
            "question_text, payload, status, generation, content_hash) "
            "VALUES ($1,'authorization','p','{}'::jsonb,'OPEN',0,$2)",
            eid, f"h-{eid}")
        await pin_plan_to_grant(
            conn, await conn.fetchval(
                "SELECT id FROM questions WHERE execution_id=$1", eid))
        assert await standing_approval(conn, eid) is not None

        await conn.execute(
            "UPDATE kernel.errand_schedules SET revoked_at = now() "
            "WHERE id = $1", granted["schedule"])
        assert await standing_approval(conn, eid) is None, (
            "revoking took effect at the end of the run rather than the "
            "next leg")
    _run(body)
