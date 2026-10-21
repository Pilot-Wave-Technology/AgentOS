"""How long someone has been active, and how many people there are.

N IS SCOPE-DEPENDENT. Platform-wide uses everyone active anywhere;
goal-scoped uses the people active in that one goal. Using the platform
count inside a single-goal view was a real bug -- a fixed fraction of a
platform-wide N is rarely met inside one goal.
"""
from __future__ import annotations

import logging
from app.database import get_pool
from app.services.taximeter.reads import _ACTIVE_GOAL_ONLY, _VALID_METRIC_KEYS

_log = logging.getLogger(__name__)


async def _fetch_platform_active_n(pool, window_weeks: int) -> int:
    """Count of distinct users with ANY Taximeter activity (any metric, any
    goal) in their own N most recent active weeks — the population-relative
    formula's N for the platform-wide view. See `_fetch_goal_active_n` for
    the goal-scoped equivalent; `compute_activity` picks between the two."""
    row = await pool.fetchrow(
        f"""
        WITH recent_weeks AS (
            SELECT uid, week_start
              FROM (
                  SELECT uid, week_start,
                         dense_rank() OVER (PARTITION BY uid ORDER BY week_start DESC) AS recency
                    FROM (
                        SELECT DISTINCT uid, week_start FROM mv_taximeter_human_activity
                         WHERE metric_key = ANY($2::text[]) AND {_ACTIVE_GOAL_ONLY}
                    ) w
              ) ranked
             WHERE recency <= $1
        )
        SELECT count(DISTINCT uid) AS n FROM recent_weeks
        """,
        window_weeks, list(_VALID_METRIC_KEYS),
    )
    return int(row["n"]) if row else 0

async def _fetch_goal_active_n(pool, window_weeks: int, goal_id: str) -> int:
    """Count of distinct users with ANY Taximeter activity WITHIN THIS GOAL
    ONLY, in their own N most recent active weeks (scoped to weeks they
    were active on this goal, not their platform-wide active weeks) — the
    population-relative formula's N for the goal-scoped view.

    Fixed 2026-07-08: goal-scoped views previously used
    `_fetch_platform_active_n` (the whole platform's count) here, which
    made the sparsity floor trip almost every time inside a single goal
    (a goal rarely has anywhere near half of the *platform's* active
    population as its own contributors) — collapsing goal-scoped
    Oversight/Efficiency to the 1.0 baseline for nearly every goal
    regardless of that goal's own real data. Using this goal's own active
    count instead makes the floor check "did half of *this goal's*
    contributors participate," the comparison that's actually meaningful
    in a single-goal view.
    """
    row = await pool.fetchrow(
        """
        WITH recent_weeks AS (
            SELECT uid, week_start
              FROM (
                  SELECT uid, week_start,
                         dense_rank() OVER (PARTITION BY uid ORDER BY week_start DESC) AS recency
                    FROM (
                        SELECT DISTINCT uid, week_start FROM mv_taximeter_human_activity
                         WHERE goal_id = $1 AND metric_key = ANY($3::text[])
                    ) w
              ) ranked
             WHERE recency <= $2
        )
        SELECT count(DISTINCT uid) AS n FROM recent_weeks
        """,
        goal_id, window_weeks, list(_VALID_METRIC_KEYS),
    )
    return int(row["n"]) if row else 0

async def _fetch_active_weeks(
    pool, window_weeks: int, goal_id: str | None = None,
) -> dict[str, int]:
    """{uid: count of their own N most recent distinct active weeks} — the
    rate denominator. A week counts as "active" if the user has ANY row for
    ANY currently-scored metric that week (see `_VALID_METRIC_KEYS` — a
    week where someone's only activity was a since-removed metric doesn't
    count), not just the specific metric being rated. Only activity tied
    to a currently active goal counts (see `_ACTIVE_GOAL_ONLY`)."""
    rows = await pool.fetch(
        f"""
        SELECT uid, count(*) AS active_weeks
          FROM (
              SELECT uid, week_start,
                     dense_rank() OVER (PARTITION BY uid ORDER BY week_start DESC) AS recency
                FROM (
                    SELECT DISTINCT uid, week_start
                      FROM mv_taximeter_human_activity
                     WHERE ($1::text IS NULL OR goal_id = $1)
                       AND metric_key = ANY($3::text[])
                       AND {_ACTIVE_GOAL_ONLY}
                ) distinct_weeks
          ) ranked
         WHERE recency <= $2
         GROUP BY uid
        """,
        goal_id, window_weeks, list(_VALID_METRIC_KEYS),
    )
    return {row["uid"]: int(row["active_weeks"]) for row in rows if row["uid"]}
