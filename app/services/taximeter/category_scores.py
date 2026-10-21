"""The three categories: Oversight, Throughput, Efficiency."""
from __future__ import annotations

import logging

from app.services.taximeter.categories import BASELINE_SCORE
from app.services.taximeter.metrics import TAXIMETER_METRICS
from app.services.taximeter.metric_scores import _compute_metric_population_scores
from app.services.taximeter.normalise import _share_complement_scores
from app.services.taximeter.reads import _fetch_window_sum
from app.services.taximeter.window import _fetch_active_weeks

_log = logging.getLogger(__name__)


async def _compute_oversight_scores(
    pool,
    window_weeks: int,
    goal_id: str | None,
    active_weeks: dict[str, int],
    active_n: int,
    goal_weightages: dict[str, float],
) -> dict[str, float]:
    """Oversight = 50/50 blend of `goal_management_actions` and
    `go_thread_interactions` (added 2026-07-08), mirroring Throughput's
    blend exactly — NOT a plain sum, for the same reason (see
    `_compute_throughput_scores`'s docstring).

    `story_assignment_requests` is deliberately excluded here, not blended
    a third way: it's permanently inert (GO never sends
    `requested_by_uid`), so nobody would ever score on it — a 3-way blend
    would add a constant term of noise for no reason. Add it into this
    blend (and rebalance the split) if it ever activates for real.

    Blends with a `0.0` fill for anyone missing from one of the two
    metrics (fixed 2026-07-08, not `BASELINE_SCORE`) — see module
    docstring's "universal total" note. Filling with `1.0` would inject
    unaccounted-for credit for every person who did only one of the two
    things, breaking the "this category always sums to active_n"
    guarantee whenever the two metrics' participants don't fully overlap
    (which, unlike Throughput's two metrics, isn't structurally guaranteed
    here — `goal_management_actions` and `go_thread_interactions` come
    from unrelated event sources).
    """
    goal_mgmt_spec = TAXIMETER_METRICS["goal_management_actions"]
    thread_spec = TAXIMETER_METRICS["go_thread_interactions"]

    goal_mgmt_scores = await _compute_metric_population_scores(
        pool, goal_mgmt_spec, window_weeks, goal_id, active_weeks, active_n, goal_weightages,
    )
    thread_scores = await _compute_metric_population_scores(
        pool, thread_spec, window_weeks, goal_id, active_weeks, active_n, goal_weightages,
    )

    all_uids = set(goal_mgmt_scores) | set(thread_scores)
    blended = {
        uid: round(
            0.5 * goal_mgmt_scores.get(uid, 0.0)
            + 0.5 * thread_scores.get(uid, 0.0),
            5,
        )
        for uid in all_uids
    }
    _log.debug("oversight_scores: %d user(s) blended (goal_id=%s)", len(blended), goal_id)
    return blended

async def _compute_throughput_scores(
    pool,
    window_weeks: int,
    goal_id: str | None,
    active_weeks: dict[str, int],
    active_n: int,
    goal_weightages: dict[str, float],
) -> dict[str, float]:
    """Throughput = 50/50 blend of its two metrics' population-relative
    scores, mirroring how Oversight and Efficiency each blend their own
    two real metrics (via different mechanisms for Efficiency — see
    `_compute_efficiency_scores`) — NOT a plain sum.

    Fixed 2026-07-08: a category's score used to be the plain sum of its
    metrics' individual scores. Each metric on its own sums to exactly N
    across all participants (see `_population_relative_scores`), so
    summing two such metrics together sums to 2N, not N — found via a
    real score-sum check (3 people's Throughput summed to 6, not 3).
    Oversight looked unaffected at the time only because its second real
    metric (`go_thread_interactions`) hadn't been added yet and
    `story_assignment_requests` is permanently inert — once
    `go_thread_interactions` activated, Oversight got the same fix (see
    `_compute_oversight_scores`, added the same day).

    Blends with a `0.0` fill for anyone missing from one of the two
    metrics (fixed 2026-07-08, not `BASELINE_SCORE`) — see module
    docstring's "universal total" note. In practice Throughput's two
    metrics (`stories_completed`, `weighted_output_completed`) always
    share the same participants — both are derived from the same
    `story_completions` rows in the mat view — so this fill choice rarely
    changes a real number here, but it's what makes the "always sums to
    active_n" guarantee hold *structurally* rather than by coincidence.
    """
    stories_spec = TAXIMETER_METRICS["stories_completed"]
    weighted_spec = TAXIMETER_METRICS["weighted_output_completed"]

    stories_scores = await _compute_metric_population_scores(
        pool, stories_spec, window_weeks, goal_id, active_weeks, active_n, goal_weightages,
    )
    weighted_scores = await _compute_metric_population_scores(
        pool, weighted_spec, window_weeks, goal_id, active_weeks, active_n, goal_weightages,
    )

    all_uids = set(stories_scores) | set(weighted_scores)
    blended = {
        uid: round(
            0.5 * stories_scores.get(uid, 0.0)
            + 0.5 * weighted_scores.get(uid, 0.0),
            5,
        )
        for uid in all_uids
    }
    _log.debug("throughput_scores: %d user(s) blended (goal_id=%s)", len(blended), goal_id)
    return blended

async def _compute_efficiency_scores(
    pool, window_weeks: int, goal_id: str | None, active_n: int, active_weeks: dict[str, int],
) -> dict[str, float]:
    """Efficiency = 50/50 blend of two `_share_complement_scores` results:
    average time per story and average cost per story — both "lower is
    better" raw averages, fed directly to the share-complement transform
    rather than inverted to a points-per-unit rate first (see module
    docstring for why this replaced the earlier points-per-unit approach).
    Never goal-weighted (time/cost isn't a shared pool to redistribute).

    Blends with a `0.0` fill for anyone missing from one of the two
    metrics, same reasoning as Oversight/Throughput's blends (see module
    docstring's "universal total" note) — needed here in particular since
    time (`story_completions`, keyed by who completed the story) and cost
    (`cost_story_attribution`, keyed by who the story was assigned to) are
    genuinely different derivations with no structural guarantee of
    matching participants.
    """
    time_seconds = await _fetch_window_sum(pool, "story_completion_time_sum", window_weeks, goal_id)
    time_count = await _fetch_window_sum(pool, "story_completion_time_count", window_weeks, goal_id)
    cost_dollars = await _fetch_window_sum(pool, "cost_per_story_sum", window_weeks, goal_id)
    cost_count = await _fetch_window_sum(pool, "cost_per_story_count", window_weeks, goal_id)

    avg_time_per_story = {
        uid: time_seconds[uid] / time_count[uid]
        for uid in time_count
        if time_count.get(uid)
    }
    avg_cost_per_story = {
        uid: cost_dollars[uid] / cost_count[uid]
        for uid in cost_count
        if cost_count.get(uid)
    }
    _log.debug(
        "efficiency_scores: %d user(s) with a time sample, %d with a cost sample (goal_id=%s)",
        len(avg_time_per_story), len(avg_cost_per_story), goal_id,
    )

    time_scores = _share_complement_scores(
        avg_time_per_story, active_n, active_weeks.keys(), label="efficiency.time",
    )
    cost_scores = _share_complement_scores(
        avg_cost_per_story, active_n, active_weeks.keys(), label="efficiency.cost",
    )

    all_uids = set(time_scores) | set(cost_scores)
    blended = {
        uid: round(
            0.5 * time_scores.get(uid, 0.0)
            + 0.5 * cost_scores.get(uid, 0.0),
            5,
        )
        for uid in all_uids
    }
    _log.debug("efficiency_scores: %d user(s) blended (goal_id=%s)", len(blended), goal_id)
    return blended
