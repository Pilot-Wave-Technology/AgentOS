"""What the scorer reads, and the two filters every read shares.

SPLIT OUT OF `scoring.py`, which was 960 lines. Nothing here decides
anything -- these are the queries, kept together so the filters below
are applied in one place rather than remembered at each call site.

The two filters are the scars of real score bugs and their reasoning is
kept verbatim from the original module.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.database import get_pool
from app.services.taximeter.categories import CATEGORY_ORDER
from app.services.taximeter.metrics import TAXIMETER_METRICS

_log = logging.getLogger(__name__)
# The mat view still materializes rows for every metric_key ever added,
# including ones since removed from TAXIMETER_METRICS (decisions_made,
# task_review_actions, escalations_resolved, questions_answered,
# go_thread_interactions, story_thread_interactions) — per this module's
# own "Removing a metric" convention, those rows are just never read
# again, not cleaned up. That's fine for anything that reads one specific
# metric_key by name, but any query that means "anyone active on ANYTHING
# currently scored" (platform/goal N, active-weeks, goal/contributor
# counts) MUST filter to only the keys still in the registry, or activity
# from a retired metric silently inflates a population count that's
# supposed to reflect only what's actually being scored today. Fixed
# 2026-07-08 — found via a real score-sum mismatch (Throughput scores for
# 3 visible people summed to 8, not 3, because platform_active_n was
# picking up people whose only activity was a since-removed metric).
_VALID_METRIC_KEYS: frozenset[str] = frozenset(
    key
    for spec in TAXIMETER_METRICS.values()
    for key in (
        (f"{spec.key}_sum", f"{spec.key}_count")
        if spec.category == "efficiency"
        else (spec.key,)
    )
)

# Only activity tied to a currently-active goal counts — a goal in
# planning/paused/completed/cancelled/deleted status is either not real
# work yet or is over, and shouldn't affect anyone's current numbers.
# goal_id IS NULL is left untouched (not every row is goal-scoped) since
# "no goal" is orthogonal to "inactive goal," not a case of it.
_ACTIVE_GOAL_ONLY = "(goal_id IS NULL OR goal_id IN (SELECT id FROM goals WHERE status = 'active'))"

SORTABLE_COLUMNS: list[str] = ["overall", *CATEGORY_ORDER, "name"]

async def _fetch_metric_goal_user_totals(
    pool, metric_key: str, window_weeks: int,
) -> dict[str, dict[str, float]]:
    """{goal_id: {uid: window_total}} for one metric_key across every user
    and goal — the raw input to goal-weighted redistribution. Recency
    (last `window_weeks`) is ranked per uid across ALL their goals for this
    metric, matching the "last N active weeks" definition used everywhere
    else, while keeping each goal's own total distinct so it can be
    redistributed correctly. NULL-goal rows are excluded — there's no goal
    to redistribute a weight for."""
    rows = await pool.fetch(
        f"""
        WITH weekly AS (
            SELECT goal_id, uid, week_start, sum(value) AS value
              FROM mv_taximeter_human_activity
             WHERE metric_key = $1 AND goal_id IS NOT NULL AND {_ACTIVE_GOAL_ONLY}
             GROUP BY goal_id, uid, week_start
        ),
        ranked_weeks AS (
            SELECT uid, week_start,
                   dense_rank() OVER (PARTITION BY uid ORDER BY week_start DESC) AS recency
              FROM (SELECT DISTINCT uid, week_start FROM weekly) w
        )
        SELECT w.goal_id, w.uid, sum(w.value) AS total
          FROM weekly w
          JOIN ranked_weeks rw ON rw.uid = w.uid AND rw.week_start = w.week_start
         WHERE rw.recency <= $2
         GROUP BY w.goal_id, w.uid
        """,
        metric_key, window_weeks,
    )
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        out.setdefault(row["goal_id"], {})[row["uid"]] = float(row["total"])
    return out

async def _fetch_window_sum(
    pool, metric_key: str, window_weeks: int, goal_id: str | None = None,
) -> dict[str, float]:
    """Sum of the last `window_weeks` weekly totals per uid for one
    metric_key, collapsed across every goal the user is active in (or just
    the one goal, if `goal_id` is given). Used directly by Efficiency
    (never goal-weighted) and internally by the goal-weighted metrics'
    active-weeks/goal-count helpers below."""
    rows = await pool.fetch(
        f"""
        SELECT uid, sum(value) AS total
          FROM (
              SELECT uid, week_start, value,
                     dense_rank() OVER (PARTITION BY uid ORDER BY week_start DESC) AS recency
                FROM (
                    SELECT uid, week_start, sum(value) AS value
                      FROM mv_taximeter_human_activity
                     WHERE metric_key = $1
                       AND ($3::text IS NULL OR goal_id = $3)
                       AND {_ACTIVE_GOAL_ONLY}
                     GROUP BY uid, week_start
                ) weekly_totals
          ) recent_weeks
         WHERE recency <= $2
         GROUP BY uid
        """,
        metric_key, window_weeks, goal_id,
    )
    return {row["uid"]: float(row["total"]) for row in rows if row["uid"]}

async def _fetch_display_names(pool, uids: set[str]) -> dict[str, dict[str, str]]:
    """{uid: {"name": ..., "email": ...}}."""
    if not uids:
        return {}
    rows = await pool.fetch(
        "SELECT uid, display_name, email FROM users WHERE uid = ANY($1::text[])",
        list(uids),
    )
    return {
        row["uid"]: {
            "name": row["display_name"] or row["email"] or row["uid"],
            "email": row["email"] or "",
        }
        for row in rows
    }
