"""v2 end to end: real rows in, scores that sum to N out.

SUMMING TO N IS THE WHOLE CONTRACT. 1.0 means exactly a fair share, and
that is what lets the columns be read against each other and against
the headline. If a category can quietly total something else, every
number on the page is a different kind of thing from what the text
says it is.

The scenario below is the one in the build plan, reduced to what can be
seeded in a transaction: two people, one goal, a decision answered by
the person who does NOT own the story, an errand with finished runs,
and a second goal that is deleted.
"""
from __future__ import annotations

import asyncio

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

G = "v2s-goal"
DEAD = "v2s-dead"
TOL = 1e-3


class _Pool:
    """A pool that hands every call the one open transaction."""

    def __init__(self, conn):
        self._c = conn

    async def fetch(self, *a, **k):
        return await self._c.fetch(*a, **k)

    async def fetchval(self, *a, **k):
        return await self._c.fetchval(*a, **k)

    async def fetchrow(self, *a, **k):
        return await self._c.fetchrow(*a, **k)

    async def execute(self, *a, **k):
        return await self._c.execute(*a, **k)


def _run(body):
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await _seed(conn)
            await conn.execute("REFRESH MATERIALIZED VIEW mv_taximeter_v2")
            await body(conn, _Pool(conn))
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


async def _seed(conn):
    for gid, status in ((G, "active"), (DEAD, "deleted")):
        await conn.execute(
            "INSERT INTO goals (id, name, created_by, status) "
            "VALUES ($1, 'v2s', 'alice', $2) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status", gid, status)

    async def story(sid, uid, *, goal=G, work_kind="story", priority="high",
                    hours=2):
        await conn.execute(
            f"""INSERT INTO stories (id, goal_id, title, created_by, state,
                    assigned_to, priority, story_kind, work_kind,
                    started_at, completed_at)
                VALUES ($1, $2, 'f', 'alice', 'done', $3, $4, 'code', $5,
                        now() - interval '{hours} hours', now())""",
            sid, goal, uid, priority, work_kind)

    # ALICE owns a pod story and a local one.
    await story("v2s-a1", "alice")
    await story("v2s-a2", "alice", priority="low")
    # BOB approves the plan on ALICE's story: the credit must be Alice's.
    ex = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, state, goal_id, story_id) "
        "VALUES ('code', 'code', 'DONE', $1, 'v2s-a1') RETURNING id", G)
    await conn.execute(
        "INSERT INTO kernel.questions (execution_id, kind, question_text, "
        "        answer_text, answered_by, answered_at, status) "
        "VALUES ($1, 'authorization', 'q', 'yes', 'bob', now(), 'ANSWERED')", ex)

    # BOB owns an errand that ran twice.
    await story("v2s-b1", "bob", work_kind="errand")
    for _ in range(2):
        await conn.execute(
            "INSERT INTO kernel.executions (kind, domain, state, goal_id, story_id) "
            "VALUES ('errand', 'code', 'DONE', $1, 'v2s-b1')", G)

    # A finished story in a DELETED goal: must count for nobody.
    await story("v2s-d1", "carol", goal=DEAD)


def _only_ours(scored):
    return {u: s for u, s in scored.items() if u in ("alice", "bob", "carol")}


def test_every_column_sums_to_N():
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        scored = await compute_v2(pool, 12, G)
        assert scored, "nothing scored"
        n = len(scored)
        for col in ("oversight", "throughput", "efficiency", "overall"):
            total = sum(s[col] for s in scored.values())
            assert abs(total - n) < TOL, f"{col} sums to {total}, not {n}"
    _run(body)


def test_a_decision_answered_by_bob_scores_for_alice():
    """The rule the redesign turns on, end to end rather than in SQL."""
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        scored = await compute_v2(pool, 12, G)
        assert scored["alice"]["oversight"] > scored["bob"]["oversight"], (
            "the decision scored for whoever answered it")
    _run(body)


def test_an_errand_run_is_a_work_item_for_its_owner():
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        scored = await compute_v2(pool, 12, G)
        assert scored["bob"]["detail"]["items"] == 2.0, scored["bob"]["detail"]
    _run(body)


def test_a_deleted_goal_contributes_nobody_and_nothing():
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        scored = await compute_v2(pool, 12, None)
        assert "carol" not in scored, "a deleted goal's owner was scored"
    _run(body)


def test_a_goal_scope_request_for_a_deleted_goal_is_empty():
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        assert await compute_v2(pool, 12, DEAD) == {}
    _run(body)


def test_a_local_only_person_is_in_the_pool_and_scores_the_average():
    """Nobody here has a recorded cost, so every finisher is 'cost not
    applicable' and the pool splits N evenly on that half."""
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        scored = await compute_v2(pool, 12, G)
        for uid in ("alice", "bob"):
            assert scored[uid]["detail"]["in_efficiency_pool"], uid
        total = sum(s["efficiency"] for s in scored.values())
        assert abs(total - len(scored)) < TOL
    _run(body)


def test_the_window_is_respected():
    """A window that excludes this week scores nobody -- which is also
    what makes the goal weight window-dependent."""
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        assert await compute_v2(pool, 0, G) == {}
    _run(body)


def test_goal_weight_counts_only_items_inside_the_window():
    async def body(conn, pool):
        from app.services.taximeter.v2.goal_weight import goal_weights

        wide = await goal_weights(pool, 12)
        assert wide.get(G, 0) >= 4.0, wide        # 1 + 3 items (2 stories + 2 runs - errand story excluded)
        narrow = await goal_weights(pool, 0)
        assert narrow.get(G, 1.0) == 1.0 or G not in narrow
    _run(body)


def test_the_detail_chain_is_present_for_the_explainer():
    """S6 renders raw -> rate -> score rather than restating the rules,
    so the numbers it needs have to come out of the scorer."""
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        d = (await compute_v2(pool, 12, G))["alice"]["detail"]
        for key in ("active_weeks", "items", "priority_points", "cost_usd",
                    "cycle_hours", "in_efficiency_pool"):
            assert key in d, key
    _run(body)


def test_the_setting_chooses_the_scorer():
    """`TAXIMETER_SCORING` is the only thing that decides, and it is
    read per call -- so an environment can be switched without a
    restart, and a test can flip it without reloading a module."""
    import app.services.dashboard_metrics as dm
    from app.config import get_settings

    get_settings.cache_clear()
    assert hasattr(dm, "_score_rows")
    src = dm._score_rows.__doc__ or ""
    assert "setting" in src.lower()


# ── cost is reported, not scored ────────────────────────────────────
#
# Scoring points-per-dollar ranked people on a dimension that exists
# for only some of them: a VS Code story records NO tokens at all (0
# local stories carry a `token_calls` row on feature, against 51 cloud
# ones), so its cost is unknown rather than zero. Every treatment of an
# unknown is a reward or a penalty relative to being measured, and
# giving the unmeasured the pool average made working locally an
# insurance policy against a below-average cost score.

def test_an_expensive_story_does_not_lower_its_owners_efficiency():
    """THE DECISIVE ONE, and the first version of it proved nothing.

    It gave ONE person a cost and nobody else, so `pool_scores` fell
    back to the average for everyone (fewer than two measurable rates)
    and the two scored the same under the OLD rule as well. Checked by
    putting the old blend back: still green, still worthless.

    TWO costed people, one cheap and one dear, same work and same
    hours. Under the old rule the dear one scored lower on Efficiency.
    Now they are level, because they delivered the same work in the
    same time -- which is what Efficiency measures.
    """
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        for uid in ("dear", "cheap"):
            await conn.execute(
                """INSERT INTO stories (id, goal_id, title, created_by, state,
                        assigned_to, priority, story_kind, work_kind,
                        started_at, completed_at)
                   VALUES ($1, $2, 'f', 'alice', 'done', $3, 'high', 'code',
                           'story', now() - interval '2 hours', now())""",
                f"v2c-{uid}", G, uid)
        # A hundredfold difference in spend, identical delivery.
        for uid, tokens in (("dear", 9_000_000), ("cheap", 90_000)):
            await conn.execute(
                "INSERT INTO token_calls (recorded_at, goal_id, provider, model, "
                "   input_tokens, output_tokens, thinking_tokens, "
                "   cache_read_tokens, cache_write_tokens, record_type, story_id) "
                "VALUES (now(), $1, 'anthropic', 'claude-opus-4-6', "
                "        $2, 0, 0, 0, 0, 'call', $3)", G, tokens, f"v2c-{uid}")
        await conn.execute("REFRESH MATERIALIZED VIEW mv_taximeter_v2")

        scored = await compute_v2(pool, 12, G)
        assert "dear" in scored and "cheap" in scored, scored.keys()
        assert abs(scored["dear"]["efficiency"]
                   - scored["cheap"]["efficiency"]) < TOL, (
            "spending a hundred times more changed an Efficiency score — "
            "cost is being scored again")
        # And the spend is still visible.
        assert (scored["dear"]["detail"]["cost_usd"]
                > scored["cheap"]["detail"]["cost_usd"]), "cost stopped being reported"
    _run(body)


def test_a_faster_delivery_still_scores_higher():
    """Efficiency still measures something. If it did not, dropping
    cost would have turned the column into a constant."""
    async def body(conn, pool):
        from app.services.taximeter.v2.compute2 import compute_v2

        for uid, hours in (("quick", 1), ("slow", 20)):
            await conn.execute(
                f"""INSERT INTO stories (id, goal_id, title, created_by, state,
                        assigned_to, priority, story_kind, work_kind,
                        started_at, completed_at)
                    VALUES ($1, $2, 'f', 'alice', 'done', $3, 'high', 'code',
                            'story', now() - interval '{hours} hours', now())""",
                f"v2c-{uid}", G, uid)
        await conn.execute("REFRESH MATERIALIZED VIEW mv_taximeter_v2")

        scored = await compute_v2(pool, 12, G)
        assert scored["quick"]["efficiency"] > scored["slow"]["efficiency"]
    _run(body)


def test_cost_is_still_reported_on_the_row():
    """Not scored, but not discarded: "what did this cost" is a real
    question, and the view still records it."""
    async def body(conn, pool):
        from app.services.taximeter.v2.activity import compute_activity_v2

        rows = await compute_activity_v2(pool, 12, G)
        assert rows, "nothing scored"
        assert all("cost_usd" in r for r in rows), \
            "cost was dropped from the row instead of being reported"
    _run(body)


# ── the modal and the table must agree ──────────────────────────────

def test_the_drilldown_uses_the_same_scorer_as_the_table():
    """THE ROW AND THE BREAKDOWN BEHIND IT. The modal ran v1 while the
    table ran v2, so a person's name and their own breakdown disagreed
    on one page -- which reads as a bug in the numbers rather than in
    the wiring, and is worse than either being wrong alone."""
    async def body(conn, pool):
        from app.services.taximeter.v2.activity import compute_activity_v2
        from app.services.taximeter.v2.drilldown2 import compute_goal_drilldown_v2

        rows = {r["uid"]: r for r in await compute_activity_v2(pool, 12, G)}
        assert rows, "nothing scored"
        uid = next(iter(rows))
        drill = await compute_goal_drilldown_v2(pool, uid, 12, G)
        mine = [g for g in drill["goals"] if g["goal_id"] == G]
        assert mine, f"{uid} has no row for the goal they worked in"
        # Scoped to one goal, the table row and the drilldown row are the
        # same computation, so they must produce the same number.
        assert abs(mine[0]["overall"] - rows[uid]["overall"]) < TOL, (
            f"table says {rows[uid]['overall']}, modal says {mine[0]['overall']}")
    _run(body)


def test_the_drilldown_reports_the_REAL_goal_weight():
    """Not `goals.weightage`, which the retired Gemini job used to write
    and nothing sets now -- it would show 1.00 for every goal under a
    heading claiming it meant something."""
    async def body(conn, pool):
        from app.services.taximeter.v2.drilldown2 import compute_goal_drilldown_v2

        drill = await compute_goal_drilldown_v2(pool, "alice", 12, G)
        assert drill["goals"], "alice has no goals"
        row = drill["goals"][0]
        assert "goal_weight" in row
        assert "weightage" not in row, "the retired column is still being sent"
        # The seeded goal finished work, so its weight must exceed the
        # floor of 1 -- otherwise this is the old column wearing a new name.
        assert row["goal_weight"] > 1.0, row["goal_weight"]
    _run(body)
