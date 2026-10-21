"""Goals: which ones count, what they weigh, and who works in them."""
from __future__ import annotations

import logging
from app.database import get_pool
from app.services.taximeter.reads import _ACTIVE_GOAL_ONLY, _VALID_METRIC_KEYS

_log = logging.getLogger(__name__)


async def fetch_taximeter_goals(pool=None) -> list[dict]:
    """Currently active goals with any recorded Taximeter activity, for the
    dashboard's goal picker — not every goal in the platform, just ones with
    something to show if selected."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT m.goal_id, g.name
          FROM mv_taximeter_human_activity m
          JOIN goals g ON g.id = m.goal_id
         WHERE g.status = 'active'
         ORDER BY g.name
        """
    )
    return [dict(r) for r in rows]

async def _fetch_goal_weightages(pool) -> dict[str, float]:
    """{goal_id: weightage} for every active goal — the per-goal redistribution
    weight in the shared formula. Defaults to 1.0 (see migrations/
    goal_weightage.sql) until the LLM-based holistic recompute sets real
    values."""
    rows = await pool.fetch("SELECT id, weightage FROM goals WHERE status = 'active'")
    return {row["id"]: float(row["weightage"]) for row in rows}

async def _fetch_active_goal_count(pool, window_weeks: int) -> dict[str, int]:
    """{uid: count of distinct active goals they had any activity in, during
    their own N most recent active weeks} — always platform-wide, never
    scoped by the panel's goal filter. The breadth/depth signal shown next
    to each name in the main table."""
    rows = await pool.fetch(
        f"""
        WITH recent_weeks AS (
            SELECT uid, week_start
              FROM (
                  SELECT uid, week_start,
                         dense_rank() OVER (PARTITION BY uid ORDER BY week_start DESC) AS recency
                    FROM (
                        SELECT DISTINCT uid, week_start FROM mv_taximeter_human_activity
                         WHERE metric_key = ANY($2::text[])
                    ) w
              ) ranked
             WHERE recency <= $1
        )
        SELECT m.uid, count(DISTINCT m.goal_id) AS goal_count
          FROM mv_taximeter_human_activity m
          JOIN recent_weeks rw ON rw.uid = m.uid AND rw.week_start = m.week_start
         WHERE m.goal_id IS NOT NULL
           AND m.goal_id IN (SELECT id FROM goals WHERE status = 'active')
           AND m.metric_key = ANY($2::text[])
         GROUP BY m.uid
        """,
        window_weeks, list(_VALID_METRIC_KEYS),
    )
    return {row["uid"]: int(row["goal_count"]) for row in rows if row["uid"]}

async def _fetch_contributor_counts(pool, window_weeks: int) -> dict[str, int]:
    """{goal_id: count of distinct users who had any activity in it, during
    the goal's own N most recent active weeks}. Shown next to each goal on
    the Goals tab's landing list."""
    rows = await pool.fetch(
        """
        WITH recent_weeks AS (
            SELECT goal_id, week_start
              FROM (
                  SELECT goal_id, week_start,
                         dense_rank() OVER (PARTITION BY goal_id ORDER BY week_start DESC) AS recency
                    FROM (
                        SELECT DISTINCT goal_id, week_start
                          FROM mv_taximeter_human_activity
                         WHERE goal_id IS NOT NULL AND metric_key = ANY($2::text[])
                    ) w
              ) ranked
             WHERE recency <= $1
        )
        SELECT m.goal_id, count(DISTINCT m.uid) AS contributor_count
          FROM mv_taximeter_human_activity m
          JOIN recent_weeks rw ON rw.goal_id = m.goal_id AND rw.week_start = m.week_start
         WHERE m.goal_id IS NOT NULL
           AND m.goal_id IN (SELECT id FROM goals WHERE status = 'active')
           AND m.metric_key = ANY($2::text[])
         GROUP BY m.goal_id
        """,
        window_weeks, list(_VALID_METRIC_KEYS),
    )
    return {row["goal_id"]: int(row["contributor_count"]) for row in rows if row["goal_id"]}

async def fetch_goal_list_with_contributors(window_weeks: int = 12) -> list[dict]:
    """The Goals tab's landing list — every goal with any activity in the
    window, plus its contributor count. Selecting one re-runs
    `compute_activity(window_weeks, goal_id)` scoped to just that goal."""
    pool = get_pool()
    contributor_counts = await _fetch_contributor_counts(pool, window_weeks)
    names = await _fetch_goal_names(pool, set(contributor_counts))
    rows = [
        {
            "goal_id": goal_id,
            "name": names.get(goal_id, "(deleted goal)"),
            "contributor_count": count,
        }
        for goal_id, count in contributor_counts.items()
    ]
    rows.sort(key=lambda row: row["name"].lower())
    _log.debug(
        "taximeter: goal list for Goals tab has %d goals (window=%dw)", len(rows), window_weeks,
    )
    return rows

async def _fetch_goal_names(pool, goal_ids: set[str]) -> dict[str, str]:
    real_ids = {goal_id for goal_id in goal_ids if goal_id and goal_id != "(no goal)"}
    if not real_ids:
        return {}
    rows = await pool.fetch(
        "SELECT id, name FROM goals WHERE id = ANY($1::text[])", list(real_ids),
    )
    return {row["id"]: row["name"] for row in rows}

async def _fetch_user_active_goal_ids(pool, uid: str, window_weeks: int) -> list[tuple[str, str]]:
    """[(goal_id, goal_name)] this person had any activity in, during their
    own N most recent active weeks — the drill-down table's row list."""
    rows = await pool.fetch(
        """
        WITH recent_weeks AS (
            SELECT week_start
              FROM (
                  SELECT week_start,
                         dense_rank() OVER (ORDER BY week_start DESC) AS recency
                    FROM (
                        SELECT DISTINCT week_start FROM mv_taximeter_human_activity
                         WHERE uid = $1 AND metric_key = ANY($3::text[])
                    ) w
              ) ranked
             WHERE recency <= $2
        )
        SELECT DISTINCT m.goal_id, g.name
          FROM mv_taximeter_human_activity m
          JOIN recent_weeks rw ON rw.week_start = m.week_start
          JOIN goals g ON g.id = m.goal_id AND g.status = 'active'
         WHERE m.uid = $1 AND m.goal_id IS NOT NULL
           AND m.metric_key = ANY($3::text[])
         ORDER BY g.name
        """,
        uid, window_weeks, list(_VALID_METRIC_KEYS),
    )
    return [(row["goal_id"], row["name"]) for row in rows]
