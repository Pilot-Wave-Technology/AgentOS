"""Executor spend from `step_journal` into the cost ledger.

A DRAIN, NOT A HOOK. The obvious alternative is to write the ledger row from
the harness as each step is journaled, and it is worse in three ways that all
matter here. It puts a second write on the hot path of every model call. It
puts a cost-reporting failure inside a leg, where it can end work that was
going fine. And it can only ever record the future, leaving the 2,268 steps
already on disk permanently invisible.

Draining is idempotent, catches history for free, and cannot fail a leg: the
worst a broken drain does is leave the ledger behind, which is visible and
fixable. The same pass is therefore both the live path and the backfill --
there is no separate backfill script to run once, get wrong, and re-run.

WHY NOT A HIGH-WATER MARK. A cursor per execution would be smaller, but it
has to be stored somewhere and kept correct across crashes, concurrent
workers and re-runs. The unique `dedupe_key` already makes a re-read
harmless, so the cheap and safe thing is to re-read a bounded window and let
the index reject what is already counted.
"""
from __future__ import annotations

import logging

from kernel.usage.sink import record, run_scope, usage_of

logger = logging.getLogger(__name__)

# Rows whose spend is real but whose journal entry the ticker hides. They are
# still MODEL CALLS -- `boundary_prompt` alone is the harness asking the model
# whether it is done, on every leg -- so hiding them from a person watching is
# right and dropping them from the accounts is not.
_LOOKBACK = """
    SELECT j.execution_id, j.seq, j.cost, j.ts
      FROM step_journal j
     WHERE j.cost IS NOT NULL
       AND j.ts > now() - ($1::text || ' hours')::interval
     ORDER BY j.execution_id, j.seq
     LIMIT $2
"""

_ALL = """
    SELECT j.execution_id, j.seq, j.cost, j.ts
      FROM step_journal j
     WHERE j.cost IS NOT NULL AND j.execution_id > $1
     ORDER BY j.execution_id, j.seq
     LIMIT $2
"""


def key_for(execution_id: int, seq: int) -> str:
    """Stable across re-runs, and that is the whole point of it."""
    return f"exec:{execution_id}:{seq}"


async def drain_executor(conn, *, hours: int = 6, limit: int = 5000) -> int:
    """Ledger rows written for executor spend in the recent window.

    The window is generous on purpose. A leg that ran while the drain was
    down must still be picked up on the next tick, and re-reading a few
    thousand already-counted rows costs one index probe each.
    """
    rows = await conn.fetch(_LOOKBACK, str(int(hours)), int(limit))
    return await _write(conn, rows)


async def backfill_executor(conn, *, after_execution: int = 0,
                            limit: int = 20000) -> int:
    """Every executor step ever journaled. Safe to run repeatedly.

    Separate from `drain_executor` only in what it selects: the same key
    means a step already drained is rejected by the index, so running this
    over a live system double-counts nothing.
    """
    rows = await conn.fetch(_ALL, int(after_execution), int(limit))
    return await _write(conn, rows)


async def _write(conn, rows) -> int:
    written, scopes = 0, {}
    for r in rows:
        usage = usage_of(r["cost"])
        if usage is None:
            continue
        eid = r["execution_id"]
        if eid not in scopes:
            scopes[eid] = await run_scope(conn, eid)
        goal_id, story_id = scopes[eid]
        if await record(conn, execution_id=eid, role="executor",
                        dedupe_key=key_for(eid, r["seq"]), usage=usage,
                        goal_id=goal_id, story_id=story_id, at=r["ts"]):
            written += 1
    if written:
        logger.info("usage drain: %s executor row(s) ledgered", written)
    return written
