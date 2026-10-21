"""How much a goal's work counts for, computed from the work itself.

REPLACES AN LLM. `goals.weightage` was set by a Gemini job that read a
goal's description and guessed a number: unexplainable, unstable
between runs, and costing a model call to answer a question the data
already answers.

`1 + items completed in the goal during the window`. The 1 is so a goal
that finished nothing still counts for something -- the decisions made
in it were still decisions -- and the rest is the only evidence there
is about how much a goal mattered this period: how much came out of it.

LIVE FOR THE WINDOW BEING VIEWED, not stored. A stored weight is a
fourth thing to keep in step with the window, and the question "how
much did this goal deliver in the last W weeks" has a different answer
for every W.
"""

from __future__ import annotations


async def goal_weights(pool, window_weeks: int) -> dict[str, float]:
    """`{goal_id: 1 + items completed in the window}`."""
    rows = await pool.fetch(
        """
        SELECT goal_id, COALESCE(sum(value), 0) AS items
          FROM mv_taximeter_v2
         WHERE metric_key = 'items'
           AND week_start >= (date_trunc('week', now())::date
                              - ($1::int - 1) * INTERVAL '7 days')
         GROUP BY goal_id
        """, window_weeks)
    return {r["goal_id"]: 1.0 + float(r["items"]) for r in rows}


def weight_for(weights: dict[str, float], goal_id: str) -> float:
    """A goal nobody delivered in still weighs 1, not 0.

    Zero would erase every decision made in a goal that happened to
    finish nothing this period -- which is the period a goal is most
    likely to be hard, not least likely to matter.
    """
    return weights.get(goal_id, 1.0)
