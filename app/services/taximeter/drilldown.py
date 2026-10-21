"""One person's score, broken down by goal."""
from __future__ import annotations

import logging
from app.database import get_pool
from app.services.taximeter.categories import CATEGORY_ORDER, CATEGORY_WEIGHTS
from app.services.taximeter.metrics import TAXIMETER_METRICS
from app.services.taximeter.compute import compute_activity
from app.services.taximeter.goals import (
    _fetch_goal_names,
    _fetch_goal_weightages,
    _fetch_user_active_goal_ids,
)
from app.services.taximeter.reads import _ACTIVE_GOAL_ONLY, _VALID_METRIC_KEYS
from app.services.taximeter.window import _fetch_active_weeks

_log = logging.getLogger(__name__)


async def compute_goal_drilldown(uid: str, window_weeks: int = 12) -> dict:
    """Per-goal score breakdown for one person — the drill-down table:
    one row per goal they're active in, with that goal's own Oversight/
    Throughput/Efficiency/Overall scores (the same goal-scoped computation
    the dashboard's goal picker uses), replacing v4's bar chart. Layout
    resolved 2026-07-08 — see the redesign doc.

    Runs one `compute_activity(goal_id=...)` call per goal the person is
    active in — fine at the scale of goals-per-person seen so far, worth
    revisiting if that count grows large enough to matter.
    """
    pool = get_pool()
    goal_ids = await _fetch_user_active_goal_ids(pool, uid, window_weeks)
    goal_weightages = await _fetch_goal_weightages(pool)
    _log.debug(
        "taximeter: goal drilldown uid=%s spans %d goals (window=%dw)",
        uid, len(goal_ids), window_weeks,
    )

    rows = []
    for goal_id, goal_name in goal_ids:
        scoped = await compute_activity(window_weeks, goal_id=goal_id)
        match = next((r for r in scoped if r["uid"] == uid), None)
        if not match:
            continue
        rows.append({
            "goal_id": goal_id,
            "goal_name": goal_name,
            "weightage": goal_weightages.get(goal_id, 1.0),
            "oversight": match["oversight"],
            "throughput": match["throughput"],
            "efficiency": match["efficiency"],
            "overall": match["overall"],
        })
    _log.info(
        "taximeter: goal drilldown uid=%s returned %d/%d goal row(s) (window=%dw)",
        uid, len(rows), len(goal_ids), window_weeks,
    )
    return {"uid": uid, "goals": rows}
