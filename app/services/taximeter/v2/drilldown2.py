"""One person's v2 score, goal by goal, with the chain behind each number.

THE MODAL WAS STILL RUNNING v1 WHILE THE TABLE RAN v2. The row said
one thing and the breakdown behind it said another, on the same page,
from two different scorers -- which is worse than either being wrong,
because the disagreement looks like a bug in the numbers rather than
in the wiring.

AND THE "GOAL WEIGHT" COLUMN SHOWED THE RETIRED ONE. The header was
renamed to "Goal weight" in S6 while the cell still rendered
`goals.weightage`, the column the Gemini job used to write and nothing
sets any more -- so it showed 1.00 for every goal under a label
claiming it meant something.

RAW -> RATE -> SCORE rides along per goal, so the explainer can show
where a number came from instead of restating the rules.
"""

from __future__ import annotations

import logging

from app.services.taximeter.v2.compute2 import compute_v2
from app.services.taximeter.v2.goal_weight import goal_weights, weight_for

_log = logging.getLogger(__name__)


async def compute_goal_drilldown_v2(pool, uid: str,
                                    window_weeks: int = 12,
                                    goal_id: str | None = None) -> dict:
    """`{uid, goals: [{goal_id, goal_name, goal_weight, ...scores, detail}]}`.

    One row per goal this person was active in, each scored AS IF that
    goal were the only one -- which is what makes the rows comparable
    with each other, and is the same thing v1 did.
    """
    weights = await goal_weights(pool, window_weeks)
    rows = []
    for gid, name in await _active_goals(pool, uid, window_weeks, goal_id):
        scoped = await compute_v2(pool, window_weeks, gid)
        mine = scoped.get(uid)
        if not mine:
            continue
        rows.append({
            "goal_id": gid,
            "goal_name": name,
            # THE REAL ONE: 1 + items finished in this goal this period.
            "goal_weight": round(weight_for(weights, gid), 2),
            "oversight": mine["oversight"],
            "throughput": mine["throughput"],
            "efficiency": mine["efficiency"],
            "overall": mine["overall"],
            "detail": mine["detail"],
        })
    _log.info("taximeter v2: drilldown uid=%s -> %d goal row(s)", uid, len(rows))
    return {"uid": uid, "goals": rows}


async def _active_goals(pool, uid: str, window_weeks: int,
                        goal_id: str | None) -> list[tuple[str, str]]:
    """The goals this person has a scored row in, newest activity first.

    Read from the view rather than from `goal_members`: being a member
    of a goal is not the same as having done anything in it, and a row
    of zeros for every goal somebody was added to would bury the ones
    they worked in.
    """
    rows = await pool.fetch(
        """
        SELECT v.goal_id, COALESCE(g.name, v.goal_id) AS name,
               max(v.week_start) AS last_seen
          FROM mv_taximeter_v2 v
          LEFT JOIN goals g ON g.id = v.goal_id
         WHERE v.uid = $1
           AND ($2::text IS NULL OR v.goal_id = $2)
           AND v.week_start >= (date_trunc('week', now())::date
                                - ($3::int - 1) * INTERVAL '7 days')
         GROUP BY v.goal_id, g.name
         ORDER BY last_seen DESC
        """, uid, goal_id, window_weeks)
    return [(r["goal_id"], r["name"]) for r in rows]
