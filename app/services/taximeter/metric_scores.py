"""One metric, scored across a population.

THE STEP EVERY CATEGORY SHARES: redistribute each goal's weight among
its contributors in proportion to their share of that goal's raw total,
sum a person's goal-weighted contributions, divide by their OWN active
weeks, then run the population-relative ratio on that rate.
"""
from __future__ import annotations

import logging

from app.services.taximeter.metrics import MetricSpec
from app.services.taximeter.goals import _fetch_goal_weightages
from app.services.taximeter.normalise import _population_relative_scores
from app.services.taximeter.reads import _fetch_metric_goal_user_totals
from app.services.taximeter.window import _fetch_active_weeks

_log = logging.getLogger(__name__)


async def _compute_metric_population_scores(
    pool,
    spec: MetricSpec,
    window_weeks: int,
    goal_id: str | None,
    active_weeks: dict[str, int],
    active_n: int,
    goal_weightages: dict[str, float],
) -> dict[str, float]:
    """One Oversight/Throughput metric -> {uid: population-relative score}.

    Platform-wide (goal_id=None): redistributes each goal's weight among
    its contributors before summing across a person's goals (see module
    docstring). Goal-scoped: no redistribution needed, just that goal's
    own raw values directly.
    """
    goal_user_totals = await _fetch_metric_goal_user_totals(pool, spec.key, window_weeks)
    _log.debug(
        "metric_population_scores[%s]: %d goal(s) have raw activity (goal_id=%s)",
        spec.key, len(goal_user_totals), goal_id,
    )

    if goal_id:
        per_user_raw = goal_user_totals.get(goal_id, {})
        rates = {
            uid: value / active_weeks.get(uid, 1)
            for uid, value in per_user_raw.items()
        }
    else:
        weighted_totals: dict[str, float] = {}
        for gid, per_user in goal_user_totals.items():
            goal_total = sum(per_user.values())
            if not goal_total:
                continue
            weight = goal_weightages.get(gid, 1.0)
            for uid, value in per_user.items():
                weighted_totals[uid] = weighted_totals.get(uid, 0.0) + weight * (value / goal_total)
        rates = {
            uid: total / active_weeks.get(uid, 1)
            for uid, total in weighted_totals.items()
        }

    scores = _population_relative_scores(rates, active_n, active_weeks.keys(), label=spec.key)
    _log.debug(
        "metric_population_scores[%s]: %d user(s) scored (goal_id=%s)",
        spec.key, len(scores), goal_id,
    )
    return scores
