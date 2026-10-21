"""v2's `compute_activity`: the same rows the panel already renders.

THE SHAPE IS v1'S, DELIBERATELY. The template, the sort links and the
goal picker all read `uid / name / email / goal_count / overall / one
key per category`, and S4 is about changing what the numbers MEAN, not
what the page does with them. Changing both at once would mean a
difference on screen could be either.

`detail` rides along for S6's explainer -- raw value, rate and score
per metric -- and nothing renders it yet.
"""

from __future__ import annotations

import logging

from app.services.taximeter.categories import CATEGORY_ORDER
from app.services.taximeter.reads import SORTABLE_COLUMNS, _fetch_display_names
from app.services.taximeter.v2.compute2 import compute_v2

_log = logging.getLogger(__name__)


async def compute_activity_v2(pool, window_weeks: int = 12,
                              goal_id: str | None = None,
                              sort: str = "overall",
                              direction: str = "desc") -> list[dict]:
    scored = await compute_v2(pool, window_weeks, goal_id)
    if not scored:
        return []

    names = await _fetch_display_names(pool, set(scored))
    goal_counts = await _goal_counts(pool, window_weeks, goal_id)

    rows = []
    for uid, s in scored.items():
        identity = names.get(uid, {"name": uid, "email": ""})
        rows.append({
            "uid": uid,
            "name": identity["name"],
            "email": identity["email"],
            "goal_count": goal_counts.get(uid, 0),
            "overall": s["overall"],
            **{c: s[c] for c in CATEGORY_ORDER},
            # REPORTED, NOT SCORED. A number beside the scores, because
            # "what did this cost" is a real question that is not the
            # same question as "how is this person doing" -- and a VS
            # Code story records no tokens, so scoring it would rank
            # people on a measurement that exists for only some of them.
            "cost_usd": s["detail"]["cost_usd"],
            "detail": s["detail"],
        })

    key = sort if sort in SORTABLE_COLUMNS else "overall"
    reverse = direction != "asc"
    rows.sort(key=(lambda r: r["name"].lower()) if key == "name"
              else (lambda r: r[key]), reverse=reverse)
    _log.info("taximeter v2: %d rows (window=%dw goal=%s)",
              len(rows), window_weeks, goal_id or "*")
    return rows


async def _goal_counts(pool, window_weeks: int,
                       goal_id: str | None) -> dict[str, int]:
    """How many goals each person was active in, for the row."""
    rows = await pool.fetch(
        """
        SELECT uid, count(DISTINCT goal_id) AS goals
          FROM mv_taximeter_v2
         WHERE ($1::text IS NULL OR goal_id = $1)
           AND week_start >= (date_trunc('week', now())::date
                              - ($2::int - 1) * INTERVAL '7 days')
         GROUP BY uid
        """, goal_id, window_weeks)
    return {r["uid"]: int(r["goals"]) for r in rows if r["uid"]}
