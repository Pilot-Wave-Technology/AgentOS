"""Scoring a population: who is in it, over what window, and the overall."""
from __future__ import annotations

import logging

from app.database import get_pool
from app.services.taximeter.categories import (
    BASELINE_SCORE,
    CATEGORY_ORDER,
    CATEGORY_WEIGHTS,
)
from app.services.taximeter.category_scores import (
    _compute_efficiency_scores,
    _compute_oversight_scores,
    _compute_throughput_scores,
)
from app.services.taximeter.goals import (
    _fetch_active_goal_count,
    _fetch_goal_weightages,
)
from app.services.taximeter.reads import SORTABLE_COLUMNS, _fetch_display_names
from app.services.taximeter.window import (
    _fetch_active_weeks,
    _fetch_goal_active_n,
    _fetch_platform_active_n,
)

_log = logging.getLogger(__name__)


async def compute_activity(
    window_weeks: int = 12, goal_id: str | None = None,
    sort: str = "overall", direction: str = "desc",
) -> list[dict]:
    """Every human user's Taximeter score over the last `window_weeks`,
    optionally scoped to one goal.

    Returns one dict per user — display name, active goal count, `overall`
    (CATEGORY_WEIGHTS-weighted sum of the three category scores), and one
    population-relative score per category. Sorted by `sort` (one of
    `SORTABLE_COLUMNS`; falls back to "overall") in `direction` ("asc" or
    "desc", defaulting to "desc" for every column except "name").
    """
    pool = get_pool()
    _log.debug(
        "taximeter: computing activity window=%dw goal_id=%s sort=%s dir=%s",
        window_weeks, goal_id, sort, direction,
    )

    active_weeks = await _fetch_active_weeks(pool, window_weeks, goal_id)
    goal_counts = await _fetch_active_goal_count(pool, window_weeks)
    if goal_id:
        active_n = await _fetch_goal_active_n(pool, window_weeks, goal_id)
    else:
        active_n = await _fetch_platform_active_n(pool, window_weeks)
    goal_weightages = await _fetch_goal_weightages(pool) if not goal_id else {}
    _log.debug(
        "taximeter: active_n=%d (goal_id=%s), %d users with active-week data",
        active_n, goal_id, len(active_weeks),
    )

    category_scores: dict[str, dict[str, float]] = {cat: {} for cat in CATEGORY_ORDER}
    category_scores["oversight"] = await _compute_oversight_scores(
        pool, window_weeks, goal_id, active_weeks, active_n, goal_weightages,
    )
    category_scores["throughput"] = await _compute_throughput_scores(
        pool, window_weeks, goal_id, active_weeks, active_n, goal_weightages,
    )
    category_scores["efficiency"] = await _compute_efficiency_scores(
        pool, window_weeks, goal_id, active_n, active_weeks,
    )

    uids: set[str] = set()
    for scores in category_scores.values():
        uids.update(scores)
    names = await _fetch_display_names(pool, uids)

    rows = []
    for uid in uids:
        identity = names.get(uid, {"name": uid, "email": ""})
        totals = {cat: round(category_scores[cat].get(uid, 0.0), 5) for cat in CATEGORY_ORDER}
        overall = round(sum(totals[cat] * CATEGORY_WEIGHTS[cat] for cat in CATEGORY_ORDER), 5)
        rows.append({
            "uid": uid,
            "name": identity["name"],
            "email": identity["email"],
            "goal_count": goal_counts.get(uid, 0),
            "overall": overall,
            **totals,
        })

    sort_key = sort if sort in SORTABLE_COLUMNS else "overall"
    reverse = direction != "asc"
    if sort_key == "name":
        rows.sort(key=lambda row: row["name"].lower(), reverse=reverse)
    else:
        rows.sort(key=lambda row: row[sort_key], reverse=reverse)
    _log.info(
        "taximeter: computed activity for %d users (window=%dw goal_id=%s sort=%s dir=%s)",
        len(rows), window_weeks, goal_id, sort_key, "asc" if not reverse else "desc",
    )
    return rows
