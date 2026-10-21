"""What the v2 view counts, and who it counts it for.

THE RULE BEING TESTED: everything that happens inside a story counts
for the person it is assigned to, whoever or whatever answered. The
kernel, GO, the assistant and a standing grant are workers for that
person, so a plan they approve on that person's story is that person's
decision. These tests seed each of those answerers in turn and check
the credit lands in the same place.

RAW COUNTS, NOT POINTS. The view stores `decision:<kind>` with a count
and never multiplies by a weight, so re-weighting a decision is a
Python edit rather than a migration. A test that asserted points here
would be pinning the wrong layer.

The view is refreshed inside each test's transaction and rolled back,
so these never leave rows behind.
"""
from __future__ import annotations

import asyncio

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

G = "mv2-goal"


def _run(body):
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await _reset(conn)
            await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


async def _reset(conn):
    """An active goal and two people, with no other rows in view.

    The view is global, so every assertion filters to this goal — a
    suite database carries other fixtures and a bare count would be
    measuring them.
    """
    await conn.execute(
        "INSERT INTO goals (id, name, created_by, status) "
        "VALUES ($1, 'mv2', 'alice', 'active') "
        "ON CONFLICT (id) DO UPDATE SET status = 'active'", G)


async def _story(conn, sid, *, uid="alice", state="done", kind="code",
                 work_kind="story", priority="medium", goal=G,
                 started=None, completed="now()"):
    await conn.execute(
        f"""INSERT INTO stories (id, goal_id, title, created_by, state,
                                 assigned_to, priority, story_kind, work_kind,
                                 started_at, completed_at)
            VALUES ($1, $2, 'fixture', 'alice', $3, $4, $5, $6, $7,
                    {started or 'now() - interval \'2 hours\''}, {completed})""",
        sid, goal, state, uid, priority, kind, work_kind)
    return sid


async def _refresh(conn):
    # Not CONCURRENTLY: that cannot run inside a transaction, and these
    # tests must roll back. The concurrent path is checked separately.
    await conn.execute("REFRESH MATERIALIZED VIEW mv_taximeter_v2")


async def _rows(conn, metric_prefix=""):
    return {
        (r["uid"], r["metric_key"]): float(r["value"])
        for r in await conn.fetch(
            "SELECT uid, metric_key, sum(value) AS value FROM mv_taximeter_v2 "
            " WHERE goal_id = $1 AND metric_key LIKE $2 "
            " GROUP BY uid, metric_key", G, f"{metric_prefix}%")
    }


# ── who a decision belongs to ───────────────────────────────────────

async def _execution(conn, sid, kind="code"):
    return await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, state, goal_id, story_id) "
        "VALUES ($1, 'code', 'DONE', $2, $3) RETURNING id", kind, G, sid)


def test_a_decision_answered_by_ANOTHER_PERSON_counts_for_the_owner():
    """Bob approving the plan on Alice's story is Alice's decision.

    This is the rule the whole redesign turns on, so it is tested with
    the answerer being someone other than the owner rather than with a
    convenient self-approval that would pass either way.
    """
    async def body(conn):
        await _story(conn, "mv2-s1", uid="alice")
        ex = await _execution(conn, "mv2-s1")
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, kind, question_text, "
            "        answer_text, answered_by, answered_at, status) "
            "VALUES ($1, 'authorization', 'approve?', 'yes', 'bob', now(), 'ANSWERED')", ex)
        await _refresh(conn)
        rows = await _rows(conn, "decision:")
        assert rows.get(("alice", "decision:authorization")) == 1.0
        assert not any(u == "bob" for u, _ in rows), "credit followed the answerer"
    _run(body)


def test_each_question_kind_gets_its_own_row():
    async def body(conn):
        await _story(conn, "mv2-s2", uid="alice")
        ex = await _execution(conn, "mv2-s2")
        for kind in ("authorization", "acceptance", "escalation",
                     "clarification", "direction"):
            await conn.execute(
                "INSERT INTO kernel.questions (execution_id, kind, question_text, "
                "        answer_text, answered_by, answered_at, status) "
                "VALUES ($1, $2, 'q', 'yes', 'alice', now(), 'ANSWERED')", ex, kind)
        await _refresh(conn)
        rows = await _rows(conn, "decision:")
        for kind in ("authorization", "acceptance", "escalation",
                     "clarification", "direction"):
            assert rows.get(("alice", f"decision:{kind}")) == 1.0, kind
    _run(body)


def test_an_unanswered_question_is_not_a_decision():
    async def body(conn):
        await _story(conn, "mv2-s3", uid="alice")
        ex = await _execution(conn, "mv2-s3")
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, kind, question_text, status) "
            "VALUES ($1, 'authorization', 'q', 'OPEN')", ex)
        await _refresh(conn)
        assert await _rows(conn, "decision:") == {}
    _run(body)


def test_machine_proposals_are_not_decisions():
    """`completion` and `replan` are bookkeeping; an `answer` is already
    counted through its question. Counting them would hand points for
    the machine doing its job."""
    async def body(conn):
        await _story(conn, "mv2-s4", uid="alice")
        ex = await _execution(conn, "mv2-s4")
        for kind in ("steer", "completion", "replan", "answer", "question"):
            await conn.execute(
                "INSERT INTO kernel.proposals (actor_id, kind, idempotency_key, "
                "        execution_id, payload, status) "
                "VALUES ('a', $1, $2, $3, '{}'::jsonb, 'ACCEPTED')",
                kind, f"k-{kind}-{ex}", ex)
        await _refresh(conn)
        rows = await _rows(conn, "decision:")
        assert rows.get(("alice", "decision:steer")) == 1.0
        for kind in ("completion", "replan", "answer", "question"):
            assert ("alice", f"decision:{kind}") not in rows, kind
    _run(body)


def test_a_question_on_a_run_with_no_story_counts_for_nobody():
    """Four of these exist on next. They have no owner, so they are
    nobody's decision -- not the platform's average, not the last
    person to touch the goal."""
    async def body(conn):
        ex = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, state, goal_id) "
            "VALUES ('code', 'code', 'DONE', $1) RETURNING id", G)
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, kind, question_text, "
            "        answer_text, answered_by, answered_at, status) "
            "VALUES ($1, 'authorization', 'q', 'yes', 'bob', now(), 'ANSWERED')", ex)
        await _refresh(conn)
        assert await _rows(conn, "decision:") == {}
    _run(body)


def test_a_local_plan_decision_counts():
    async def body(conn):
        await _story(conn, "mv2-s5", uid="alice")
        await conn.execute(
            "INSERT INTO ledger_events (event_id, event_type, actor_id, actor_type, "
            "        timestamp, payload, context_goal_id, integrity_hash) "
            "VALUES (gen_random_uuid(), 'local_plan.decided', gen_random_uuid(), "
            "        'human', now(), $1::jsonb, $2, 'x')",
            '{"story_id": "mv2-s5", "verdict": "approved"}', G)
        await _refresh(conn)
        assert (await _rows(conn, "decision:")).get(
            ("alice", "decision:local_plan")) == 1.0
    _run(body)


# ── work items ──────────────────────────────────────────────────────

def test_a_merge_story_is_not_a_work_item_but_its_approval_counts():
    """Merge stories integrate work that was already counted. Counting
    them again would pay twice for one piece of work."""
    async def body(conn):
        await _story(conn, "mv2-m1", uid="alice", kind="merge")
        await conn.execute(
            "UPDATE stories SET merge_approved_at = now() WHERE id = 'mv2-m1'")
        await _refresh(conn)
        items = await _rows(conn, "items")
        assert items.get(("alice", "items")) is None, "a merge story was counted"
        assert (await _rows(conn, "decision:")).get(
            ("alice", "decision:merge")) == 1.0
    _run(body)


def test_priority_points_use_the_normalised_weight():
    async def body(conn):
        await _story(conn, "mv2-p1", uid="alice", priority="critical")
        await _story(conn, "mv2-p2", uid="alice", priority="low")
        await _refresh(conn)
        rows = await _rows(conn, "priority_points")
        assert rows.get(("alice", "priority_points")) == 9.0   # 8 + 1
    _run(body)


def test_an_odd_spelling_still_scores_as_medium():
    """Rows written before S1 normalised them."""
    async def body(conn):
        await conn.execute(
            "ALTER TABLE stories DROP CONSTRAINT IF EXISTS stories_priority_valid")
        await _story(conn, "mv2-p3", uid="alice", priority="blocker")
        await _refresh(conn)
        assert (await _rows(conn, "priority_points")).get(
            ("alice", "priority_points")) == 3.0
    _run(body)


def test_an_unfinished_story_is_not_an_item():
    async def body(conn):
        await _story(conn, "mv2-u1", uid="alice", state="in_progress",
                     completed="NULL")
        await _refresh(conn)
        assert await _rows(conn, "items") == {}
    _run(body)


def test_an_errand_counts_once_per_FINISHED_run():
    """A recurring errand's story is not itself an item, or it would
    count once for ever. Each finished run is."""
    async def body(conn):
        await _story(conn, "mv2-e1", uid="bob", work_kind="errand")
        for state in ("DONE", "DONE", "FAILED"):
            await conn.execute(
                "INSERT INTO kernel.executions (kind, domain, state, goal_id, story_id) "
                "VALUES ('errand', 'code', $1, $2, 'mv2-e1')", state, G)
        await _refresh(conn)
        assert (await _rows(conn, "items")).get(("bob", "items")) == 2.0
    _run(body)


# ── cycle time and cost ─────────────────────────────────────────────

def test_an_item_with_no_start_counts_as_an_item_but_not_as_hours():
    """R1. Cycle time is start -> completion; an item missing a start
    is excluded from the average rather than counted as zero hours,
    which would drag everybody's rate down silently."""
    async def body(conn):
        await _story(conn, "mv2-h1", uid="alice", started="NULL")
        await _refresh(conn)
        assert (await _rows(conn, "items")).get(("alice", "items")) == 1.0
        assert (await _rows(conn, "cycle_hours")) == {}
    _run(body)


def test_cycle_hours_are_summed_with_their_count():
    async def body(conn):
        await _story(conn, "mv2-h2", uid="alice",
                     started="now() - interval '3 hours'")
        await _refresh(conn)
        rows = await _rows(conn, "cycle_hours")
        assert rows.get(("alice", "cycle_hours_count")) == 1.0
        assert 2.9 < rows.get(("alice", "cycle_hours_sum"), 0) < 3.1
    _run(body)


def test_an_item_with_no_cost_is_absent_from_cost_rather_than_zero():
    """A local story records no spend. Counted as $0 it would make
    per-dollar efficiency infinite; it is excluded and scored neutrally
    by the scorer instead."""
    async def body(conn):
        await _story(conn, "mv2-c1", uid="alice")
        await _refresh(conn)
        assert await _rows(conn, "cost_") == {}
    _run(body)


# ── which goals count ───────────────────────────────────────────────

def test_a_goal_that_is_not_active_contributes_nothing():
    async def body(conn):
        await _story(conn, "mv2-d1", uid="alice")
        await conn.execute("UPDATE goals SET status = 'deleted' WHERE id = $1", G)
        await _refresh(conn)
        assert await _rows(conn) == {}
    _run(body)


def test_the_view_is_registered_for_refresh():
    """A view created and never added to `_MAT_VIEWS_BASE` is refreshed
    once, at creation, and then serves a frozen snapshot for ever --
    which looks like working software."""
    from app.services.dashboard_metrics import _MAT_VIEWS_BASE

    assert "mv_taximeter_v2" in _MAT_VIEWS_BASE


def test_refresh_concurrently_works():
    """Needs the unique index, and needs it to actually be unique --
    see the NOT NULL note in the migration. Outside a transaction,
    because CONCURRENTLY cannot run in one."""
    async def go():
        conn = await _connect_or_skip()
        try:
            await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_taximeter_v2")
        finally:
            await conn.close()
    asyncio.run(go())


def test_a_null_goal_cannot_reach_the_view():
    """The unique index is on (uid, goal_id, metric_key, week_start),
    and a NULL goal_id would make it stop enforcing -- two such rows
    would both be admitted and the score would double. Every source
    joins an active goal, so none can be NULL."""
    async def go():
        conn = await _connect_or_skip()
        try:
            nulls = await conn.fetchval(
                "SELECT count(*) FROM mv_taximeter_v2 WHERE goal_id IS NULL")
            assert nulls == 0, f"{nulls} rows carry a NULL goal_id"
        finally:
            await conn.close()
    asyncio.run(go())
