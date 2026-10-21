"""Cost attribution: the numbers add up, and cached tokens are cheap.

WHAT WAS WRONG. `token_calls` is the one ledger every cost view reads, and
two of the four LLM spenders never reached it. The executor's usage sat in
`kernel.step_journal.cost`; the verifier's was recorded nowhere. Because the
assistant and GO rows WERE there, the page looked complete while reporting
the two cheapest spenders, and `mv_cost_by_story` was empty because no row
had ever carried a story id.

THE TWO PROPERTIES WORTH TESTING, and neither can be checked by reading:

  IT ADDS UP. Every priced call belongs to exactly one spender and exactly
  one thread-or-unattributed, so both groupings must sum to the same total.
  This is a partition, and a partition is the kind of claim that holds right
  up until a new producer writes a row nobody classified.

  CACHED TOKENS ARE NOT INPUT TOKENS. `input` and `cache_read` are disjoint
  and priced apart -- cache read is a tenth of input on every model here. Get
  it wrong and the error GROWS as caching improves, so the better the system
  gets the more it over-reports. Measured on one real story: $4.98 claimed
  against a true $1.40.

`asyncio.run` in a sync wrapper, not `@pytest.mark.asyncio`: pytest-asyncio
is not installed here and an async test without it ERRORS rather than skips.
Every test skips cleanly when no database is reachable.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

TOL = 1e-9


def _run(coro_fn):
    async def go():
        conn = await _connect_or_skip()
        try:
            if not await conn.fetchval(
                    "SELECT 1 FROM information_schema.views "
                    "WHERE table_name = 'v_cost_reconciliation'"):
                pytest.skip("cost attribution migrations not applied here")
            return await coro_fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


# ── it adds up ───────────────────────────────────────────────────────────

def test_spenders_partition_the_total():
    """assistant + orchestrator + executor + verifier + other = total.

    Checked per goal, not just in aggregate: two goals whose errors cancel
    would pass a global check and still put the wrong number on every row.
    """
    async def check(conn):
        bad = await conn.fetch("""
            SELECT goal_id, total_cost,
                   COALESCE(assistant_cost,0) + COALESCE(orchestrator_cost,0)
                 + COALESCE(executor_cost,0)  + COALESCE(verifier_cost,0)
                 + COALESCE(other_cost,0) AS parts
              FROM v_cost_reconciliation
             WHERE abs(COALESCE(total_cost,0) - (
                       COALESCE(assistant_cost,0) + COALESCE(orchestrator_cost,0)
                     + COALESCE(executor_cost,0)  + COALESCE(verifier_cost,0)
                     + COALESCE(other_cost,0))) > $1""", TOL)
        assert not bad, [dict(r) for r in bad]
    _run(check)


def test_attributed_and_unattributed_partition_the_total():
    """A row belongs to one conversation or to none. There is no third
    place for a dollar to be, and a LEFT JOIN that dropped rows would show
    up here rather than as a total that is quietly a bit low."""
    async def check(conn):
        bad = await conn.fetch("""
            SELECT goal_id FROM v_cost_reconciliation
             WHERE abs(COALESCE(total_cost,0)
                       - (COALESCE(attributed_cost,0)
                          + COALESCE(unattributed_cost,0))) > $1""", TOL)
        assert not bad, [r["goal_id"] for r in bad]
    _run(check)


def test_the_thread_view_loses_nothing_the_ledger_has():
    """`mv_cost_by_thread` filters to attributed rows, so its total must
    equal the attributed total exactly -- not approximately, and not less."""
    async def check(conn):
        await conn.execute("SELECT public.refresh_mat_view('mv_cost_by_thread')")
        mv = await conn.fetchval("SELECT COALESCE(sum(cost),0) FROM mv_cost_by_thread")
        src = await conn.fetchval("""
            SELECT COALESCE(sum(cost),0) FROM v_cost_attributed
             WHERE thread_id IS NOT NULL AND goal_id IS NOT NULL
               AND recorded_at >= now() - interval '90 days'""")
        assert abs(float(mv) - float(src)) < 1e-6, f"mv={mv} source={src}"
    _run(check)


# ── caching ──────────────────────────────────────────────────────────────

def test_cached_tokens_are_priced_at_the_cache_rate():
    """The requirement in one assertion.

    Recomputed from the raw columns and the rate table rather than trusting
    the view's own arithmetic, and compared against what charging cache at
    the INPUT rate would have produced -- so this fails loudly if anyone
    ever collapses the two terms.
    """
    async def check(conn):
        row = await conn.fetchrow("""
            SELECT p.model, p.inp, p.outp, p.cache_read, p.cost,
                   mp.input_rate, mp.output_rate, mp.cache_read_rate
              FROM v_token_calls_priced p
              JOIN model_pricing mp ON mp.model = p.model
                                   AND mp.provider = p.provider
                                   AND mp.effective_from <= p.recorded_at
             WHERE p.cache_read > 1000 AND p.priced
             ORDER BY mp.effective_from DESC, p.cache_read DESC
             LIMIT 1""")
        if row is None:
            pytest.skip("no cached, priced calls in this database")

        correct = (row["inp"] * float(row["input_rate"])
                   + row["outp"] * float(row["output_rate"])
                   + row["cache_read"] * float(row["cache_read_rate"])) / 1e6
        naive = ((row["inp"] + row["cache_read"]) * float(row["input_rate"])
                 + row["outp"] * float(row["output_rate"])) / 1e6

        assert abs(float(row["cost"]) - correct) < 1e-9, (
            f"the view does not price cache reads at cache_read_rate: "
            f"view={row['cost']} expected={correct}")
        assert naive > correct, (
            "this fixture no longer discriminates — pick a row where the "
            "cache rate and the input rate actually differ")
    _run(check)


def test_a_model_with_no_rate_is_unpriced_not_free():
    """NULL and 0.00 must never be confused.

    An unpriced model reporting $0.00 is spend that has vanished from the
    accounts while looking like a fact. `priced` is the flag that keeps the
    difference visible on the page.
    """
    async def check(conn):
        rows = await conn.fetch(
            "SELECT cost, priced FROM v_token_calls_priced WHERE NOT priced LIMIT 20")
        if not rows:
            pytest.skip("everything in this database is priced")
        for r in rows:
            assert r["cost"] is None, "an unpriced row must cost NULL, never 0"
    _run(check)


# ── the kernel actually reaches the ledger ───────────────────────────────

def test_executor_spend_reconciles_with_the_journal_it_came_from():
    """The drain must move every billable step and change none of them."""
    async def check(conn):
        j = await conn.fetchrow("""
            SELECT count(*) n, COALESCE(sum((cost->>'input')::bigint),0) i,
                   COALESCE(sum((cost->>'output')::bigint),0) o,
                   COALESCE(sum(COALESCE((cost->>'cacheRead')::bigint,0)),0) cr
              FROM kernel.step_journal
             WHERE cost IS NOT NULL
               AND ((cost->>'input')::bigint > 0
                 OR (cost->>'output')::bigint > 0
                 OR COALESCE((cost->>'cacheRead')::bigint,0) > 0)""")
        if not j["n"]:
            pytest.skip("no journalled executor spend here")
        t = await conn.fetchrow("""
            SELECT count(*) n, COALESCE(sum(input_tokens),0) i,
                   COALESCE(sum(output_tokens),0) o,
                   COALESCE(sum(cache_read_tokens),0) cr
              FROM token_calls WHERE role = 'executor'""")
        assert (t["n"], t["i"], t["o"], t["cr"]) == (j["n"], j["i"], j["o"], j["cr"]), (
            f"journal={dict(j)} ledger={dict(t)} — run the backfill")
    _run(check)


def test_the_drain_cannot_double_count():
    """Re-running is the normal case, not the exceptional one: overlapping
    ticks, a restart, a manual backfill. The unique key is what makes a cost
    figure that creeps upward on its own impossible."""
    async def check(conn):
        from kernel.usage.drain import backfill_executor

        await conn.execute("SET search_path TO kernel, public")
        before = await conn.fetchval(
            "SELECT count(*) FROM public.token_calls WHERE role = 'executor'")
        again = await backfill_executor(conn)
        after = await conn.fetchval(
            "SELECT count(*) FROM public.token_calls WHERE role = 'executor'")
        assert again == 0 and after == before, (
            f"a re-run inserted {after - before} duplicate row(s)")
    _run(check)


def test_kernel_spend_carries_its_story_and_goal():
    """Attribution is the point. A kernel row with no story cannot reach a
    conversation, and it was the missing story id that left
    `mv_cost_by_story` empty in the first place."""
    async def check(conn):
        n = await conn.fetchval(
            "SELECT count(*) FROM token_calls WHERE role IN ('executor','verifier')")
        if not n:
            pytest.skip("no kernel spend ledgered here")
        orphan = await conn.fetchval("""
            SELECT count(*) FROM token_calls tc
             WHERE tc.role IN ('executor','verifier')
               AND tc.story_id IS NULL
               AND EXISTS (SELECT 1 FROM kernel.executions e
                            WHERE e.id = tc.execution_id
                              AND e.story_id IS NOT NULL)""")
        assert orphan == 0, (
            f"{orphan} kernel rows dropped a story id their execution had")
    _run(check)
