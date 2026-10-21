"""What an execution has spent and how much it has had to HOLD.

TWO NUMBERS THAT ARE NOT THE SAME NUMBER, and the whole reason this is
its own file is that they were once conflated. `_execution_spend` sums
every token the execution has been billed for; `_execution_peak_context`
reports the largest single request it has made. On the recorded baseline
those read 432,403 and under 20,000 -- a factor of twenty, because every
step re-sends the whole conversation and spend counts that replay.

CUMULATIVE ACROSS LEGS, AND THAT IS WHY THEY READ THE JOURNAL. The
loop's in-memory `step_records` restart at every leg boundary, so summing
them measured whichever leg happened to commit. E5c's orientation floor
is a fact about the EXECUTION -- first step to first write -- so it has
to come from the durable record, not from the one the current leg
happens to hold.

`_sum_step_spend` is the third, and it is the in-memory one: the per-leg
spend the checkpoint records. It delegates rather than repeating the
cache-weight formula -- two copies of a cost formula is one copy that
gets the next correction and one that does not, and this pair had
already diverged once.

Do not "simplify" these into one pass over the rows. They are asked at
different moments (the floor at the first commit, the checkpoint at the
boundary) and a shared cache would be a cache of a table that is still
being written.
"""

from __future__ import annotations

import json

from kernel.harness.predicates import _sum_spend


async def _execution_spend(conn, execution_id: int) -> float:
    """Total tokens journalled for this execution, all generations.

    Reads the journal rather than in-memory records because the earlier
    legs are gone by the time a later one commits — that is the whole
    reason the per-leg version was wrong.
    """
    rows = await conn.fetch(
        "SELECT cost::text FROM step_journal WHERE execution_id = $1",
        execution_id)
    total = 0.0
    for row in rows:
        if not row["cost"]:
            continue
        cost = json.loads(row["cost"])
        # NOT `thinking`. It is a subset of output — usage.ts is explicit
        # about it and every other sum in this tree excludes it — so
        # adding it here billed the same tokens twice. Invisible on
        # Gemini, which folds thinking into output and reports the field
        # as None, and live on Anthropic, which reports it separately.
        for key in ("input", "output", "cacheRead", "cacheWrite"):
            value = cost.get(key)
            if isinstance(value, (int, float)):
                total += value
    return total


async def _execution_peak_context(conn, execution_id: int) -> int:
    """The largest single request this execution has made, so far.

    THE ORIENTATION FLOOR IS A CONTEXT QUESTION. `_execution_spend`
    answers what reaching the first write COST, and on the recorded
    baseline that reads 432,403 — a figure dominated by conversation
    replay, since every step re-sends the whole conversation. Read as
    "orientation needs 432K of context" it is wrong by more than an
    order of magnitude: the largest single request in those same runs
    was under 20K.

    Leg sizing and H1b's admission check are both re-derived from the
    floor and both ask what a window must HOLD, so both need this number
    rather than the one beside it.
    """
    rows = await conn.fetch(
        "SELECT cost::text FROM step_journal WHERE execution_id = $1",
        execution_id)
    peak = 0
    for row in rows:
        if not row["cost"]:
            continue
        cost = json.loads(row["cost"])
        held = sum(v for v in (cost.get(k) for k in
                               ("input", "cacheRead", "cacheWrite"))
                   if isinstance(v, (int, float)))
        peak = max(peak, int(held))
    return peak


def _sum_step_spend(step: dict) -> float:
    """Spend for one step record, at the provider's cache weights.

    Delegates to the predicate's `_sum_spend` rather than repeating the
    formula. Two copies of a cost formula is one copy that gets the next
    correction and one that does not — and this pair had already
    diverged once, when cacheRead was fixed in one place.
    """
    return _sum_spend(step.get("cost"))

