"""What the v2 scorer reads out of `mv_taximeter_v2`.

ONE VIEW, ONE SHAPE: `(uid, goal_id, metric_key, week_start, value)`.
Everything below is a different grouping of those rows, which is the
point of storing raw counts -- the scorer decides what a count is
worth, and can change its mind without a migration.

THE WINDOW IS EACH PERSON'S OWN LAST W ACTIVE WEEKS, not the last W
calendar weeks. Somebody who joined a goal halfway through the window
is measured over the weeks they were there; dividing their total by
the full window instead would make joining late look like working
less, which is the correction v1 made and v2 keeps.
"""

from __future__ import annotations


async def active_weeks(pool, window_weeks: int,
                       goal_id: str | None = None) -> dict[str, int]:
    """`{uid: how many of their own last W weeks had any activity}`."""
    rows = await pool.fetch(
        """
        SELECT uid, count(*) AS weeks FROM (
            SELECT uid, week_start,
                   dense_rank() OVER (PARTITION BY uid ORDER BY week_start DESC) AS r
              FROM (SELECT DISTINCT uid, week_start FROM mv_taximeter_v2
                     WHERE ($1::text IS NULL OR goal_id = $1)) d
        ) ranked
         WHERE r <= $2
         GROUP BY uid
        """, goal_id, window_weeks)
    return {r["uid"]: int(r["weeks"]) for r in rows if r["uid"]}


async def totals_by_person(pool, window_weeks: int, metric_prefix: str,
                           goal_id: str | None = None) -> dict[str, float]:
    """`{uid: Σ value}` for every metric key starting `metric_prefix`.

    Used for the simple metrics -- items, priority points -- where the
    goal a thing happened in does not change what it is worth.
    """
    rows = await pool.fetch(
        """
        SELECT uid, COALESCE(sum(value), 0) AS total
          FROM mv_taximeter_v2
         WHERE metric_key LIKE $1
           AND ($2::text IS NULL OR goal_id = $2)
           AND week_start >= (date_trunc('week', now())::date
                              - ($3::int - 1) * INTERVAL '7 days')
         GROUP BY uid
        """, f"{metric_prefix}%", goal_id, window_weeks)
    return {r["uid"]: float(r["total"]) for r in rows if r["uid"]}


async def points_by_person_and_goal(pool, window_weeks: int, metric_prefix: str,
                                    goal_id: str | None = None,
                                    ) -> dict[tuple[str, str], float]:
    """`{(uid, goal_id): Σ count x points}` for decisions or stewardship.

    WEIGHTED HERE, IN PYTHON, from `points.py` -- the view stores the
    raw count and knows nothing about what a kind is worth. Per goal as
    well as per person, because the goal weight is applied to each
    goal's contribution separately at system scope.
    """
    from app.services.taximeter.v2.points import decision_points

    rows = await pool.fetch(
        """
        SELECT uid, goal_id, metric_key, COALESCE(sum(value), 0) AS total
          FROM mv_taximeter_v2
         WHERE metric_key LIKE $1
           AND ($2::text IS NULL OR goal_id = $2)
           AND week_start >= (date_trunc('week', now())::date
                              - ($3::int - 1) * INTERVAL '7 days')
         GROUP BY uid, goal_id, metric_key
        """, f"{metric_prefix}%", goal_id, window_weeks)
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        if not r["uid"]:
            continue
        worth = decision_points(r["metric_key"])
        if not worth:
            continue
        key = (r["uid"], r["goal_id"])
        out[key] = out.get(key, 0.0) + float(r["total"]) * worth
    return out


async def totals_by_person_and_goal(pool, window_weeks: int, metric_key: str,
                                    goal_id: str | None = None,
                                    ) -> dict[tuple[str, str], float]:
    """`{(uid, goal_id): Σ value}` for one exact metric key."""
    rows = await pool.fetch(
        """
        SELECT uid, goal_id, COALESCE(sum(value), 0) AS total
          FROM mv_taximeter_v2
         WHERE metric_key = $1
           AND ($2::text IS NULL OR goal_id = $2)
           AND week_start >= (date_trunc('week', now())::date
                              - ($3::int - 1) * INTERVAL '7 days')
         GROUP BY uid, goal_id
        """, metric_key, goal_id, window_weeks)
    return {(r["uid"], r["goal_id"]): float(r["total"])
            for r in rows if r["uid"]}


async def people(pool, window_weeks: int,
                 goal_id: str | None = None) -> set[str]:
    """Everyone with any scored row in the window: N, for this scope.

    N IS THE SCOPE'S POPULATION. Platform-wide it is everyone active
    anywhere; inside one goal it is the people active in that goal.
    Using the platform count in a goal view was a real v1 bug -- a goal
    has far fewer contributors than the platform, so a fixed fraction
    of a platform N is rarely met inside one.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT uid FROM mv_taximeter_v2
         WHERE ($1::text IS NULL OR goal_id = $1)
           AND week_start >= (date_trunc('week', now())::date
                              - ($2::int - 1) * INTERVAL '7 days')
        """, goal_id, window_weeks)
    return {r["uid"] for r in rows if r["uid"]}
