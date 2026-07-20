"""Taximeter activity engine — v5.

Replaces v4's points-per-occurrence model with a **population-relative
formula**: score = N x individual_rate / total_rate. Baseline = 1.0 —
"your fair share of the total work." Full design history and worked
examples: `D:\\Projects\\agent-control-panel\\TAXIMETER_V5_REDESIGN.md`.

**N is scope-dependent, not always platform-wide** (fixed 2026-07-08):
platform-wide view (`goal_id=None`) uses the count of everyone active
anywhere on the platform; goal-scoped view (`goal_id` given) uses the
count of people active in *that one goal* instead. Using the platform
count inside a single-goal view was a real bug — a goal typically has
far fewer contributors than the whole platform, so a fixed fraction of
platform-wide N would rarely be met inside a single goal.
`_fetch_platform_active_n` / `_fetch_goal_active_n` compute whichever is
appropriate; `compute_activity` picks between them once and threads the
result through as `active_n` everywhere below.

**Every metric's own score** (`_compute_metric_population_scores`, shared
by Oversight and Throughput):
  1. Per goal: redistribute that goal's `weightage` among its contributors,
     proportional to each person's share of that goal's raw metric total.
     A goal's weight is never exceeded, only redistributed.
  2. Sum a person's goal-weighted contributions across all their goals.
  3. Divide by the person's own active-week count (not the fixed window
     length) to get a rate — corrects for tenure (joining a goal partway
     through the window shouldn't look artificially less active).
  4. Run the population-relative ratio on that rate.
  Skipped when scoped to a single goal (`goal_id` given) — there's only
  one goal's population to consider, so step 1's redistribution is a
  no-op; the ratio runs directly on that goal's own raw values.

**How a category combines its metrics matters, and it's not always a
plain sum** (fixed 2026-07-08): each metric's own score sums to exactly N
across all its participants (in the normal case — see each scoring
function's own docstring for the edge cases where that doesn't hold).
Summing *two* such metrics together therefore sums to 2N, not N, which
breaks the "1.0 = fair share" meaning at the category level. No category
does a plain sum of two real metrics anymore: **Oversight** blends
`goal_management_actions` and `go_thread_interactions` 50/50
(`_compute_oversight_scores`, added the same day `go_thread_interactions`
activated — `story_assignment_requests` stays out of the blend while it's
permanently inert, see that function's docstring), **Throughput** blends
its two metrics 50/50 (`_compute_throughput_scores`), and **Efficiency**
blends its own two ratios 50/50 via a different mechanism
(`_compute_efficiency_scores`) — all three found/fixed via a real
score-sum check (3 people's Throughput summed to 6, not 3, before that
particular fix).

**Universal total** (fixed 2026-07-08): all three categories sum to
exactly `active_n`, always, at both platform and goal scope. `active_n`
itself is defined as the union of everyone active on ANY valid metric
across all three categories — so "every category sums to active_n" and
"every category sums to the max headcount found across the three
categories" are the same statement; `active_n` structurally IS that max,
by construction. Getting there unconditionally (not just "usually," and
not dependent on whether two blended metrics happen to have the same
participants or enough of them) required three things together:

1. **Every per-metric formula sums to exactly `active_n` on its own**,
   regardless of how many people participate. `_population_relative_scores`
   always did. `_share_complement_scores` needed its correction factor
   changed from `n/(n-1)` to `active_n/(n-1)` to match (see that
   function's docstring).
2. **Every blend fills a missing metric with `0.0`, not `BASELINE_SCORE`.**
   Filling with `1.0` was the original design and seemed harmless, but it
   silently breaks the total whenever a category's two metrics have
   different participants: each such "partial participant" injects an
   extra `+0.5` of unaccounted-for credit. Throughput's two metrics
   always share participants by construction, so this was invisible
   there, but Oversight's and Efficiency's don't.
3. **A metric that can't compute a real per-person ratio at all pins the
   FULL active roster to baseline, not just whoever showed up with data**
   — the genuinely degenerate cases (zero participants, or
   `_share_complement_scores`'s `n=1`). Confirmed on real deployed data:
   `active_n=2`, only one person had cost/time data that goal, and pinning
   just that lone contributor left the other active person defaulting to
   `0.0` at the blend, summing to `100%` of `active_n` instead of `200%`.
   Both `_population_relative_scores` and `_share_complement_scores` now
   take an `active_uids` parameter (threaded from `active_weeks.keys()`,
   already computed for tenure-adjustment) specifically so they can do
   this — pinning is meaningless without knowing who the full roster is.

With all three in place, every blend decomposes cleanly into
`0.5 × (metric A's own total) + 0.5 × (metric B's own total) =
0.5×active_n + 0.5×active_n = active_n`, independent of participant
overlap or how sparse either metric's real data is.

**Efficiency** is additionally NOT goal-weighted (time/cost isn't a
shared pool to redistribute the way a count is) — two "lower is better"
raw averages (time per story, cost per story), each run through
`_share_complement_scores` (see that function's docstring), blended 50/50.

This is deliberately NOT the same mechanism as `_population_relative_scores`
(fixed 2026-07-08, replacing an earlier "invert to points-per-unit, then
apply the population-relative ratio" approach): inverting a raw metric
and then normalizing is fine, but an EARLIER attempt at handling "lower is
better" tried flipping an already-computed, unbounded, centered-at-1.0
ratio directly (`1/p`, then `1-p`) and both broke on real data — `1/p`
produced a 483x outlier for a low-sample contributor near p=0, and `1-p`
goes negative for any p>1 (confirmed on real numbers: p=1.642 -> -0.642).
`_share_complement_scores` avoids both failure modes by construction: it
operates on a genuine share of a positive total (`value / sum(values)`),
which is always in `[0, 1]` — `1 - share` can never explode and never
goes negative, regardless of how skewed the underlying values are.

**No sparsity floor (removed 2026-07-08).** An earlier version of this
formula pinned everyone to `BASELINE_SCORE` whenever fewer than half of
`active_n` showed any activity on a metric — a guard against a thin
sample producing a misleadingly large ratio. It was created specifically
because a *different, since-abandoned* formula (flipping an
already-computed ratio via `1/p`) could produce a genuine numerical
explosion (483x, on real data) for a low-sample contributor. Neither
formula in current use has that failure mode: `_population_relative_scores`
is linear and bounded at `active_n` (hit only when one person holds 100%
of the total rate); `_share_complement_scores` operates on a bounded
`[0,1]` share by construction (see that function's docstring). With the
explosion mechanism gone, the floor was only doing a softer,
non-numerical job — deciding a real, correctly-bounded ratio wasn't
"trustworthy" enough to show — which is a judgment call, not a safety
requirement, and was removed rather than kept as an unused belt-and-braces
check. It never actually triggered on real production data in the time it
existed. If a future formula reintroduces an unbounded transform, this
guard (or something like it) should come back with it — don't assume
"no floor" is safe for a formula that isn't provably bounded.

Activity is always computed live, never stored — nothing here depends on
the metric set or formula staying the same across runs.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.database import get_pool
from app.services.taximeter.categories import (
    BASELINE_SCORE,
    CATEGORY_ORDER,
    CATEGORY_WEIGHTS,
)
from app.services.taximeter.metrics import MetricSpec, TAXIMETER_METRICS

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


def _population_relative_scores(
    rates: dict[str, float], active_n: int, active_uids: Iterable[str], label: str = "metric",
) -> dict[str, float]:
    """{uid: rate} -> {uid: score}, score = N * rate / total_rate — "how
    many times your fair share of the total." `active_n` is whichever
    population this call is scoped to — platform-wide or one goal (see
    module docstring); this function itself doesn't know or care which.
    No sparsity floor (removed 2026-07-08, see module docstring) — this
    formula is linear and bounded at `active_n` regardless of how few
    people participate, so a thin sample can't produce an unbounded or
    negative result, only a larger (but capped) share of `active_n`.
    `label` is for log lines only.

    Zero participants (fixed 2026-07-08): pins EVERYONE in `active_uids`
    (the full active roster for this scope, not just whoever this metric
    found) to `BASELINE_SCORE`, not an empty dict. This matters for a
    blend (see `_compute_oversight_scores`/`_compute_throughput_scores`):
    if this metric returned `{}` and the blend 0.0-filled it, a category
    could only ever reach `0.5×active_n` when its OTHER metric has zero
    participants platform/goal-wide — pinning the full roster to baseline
    instead makes this metric contribute its full `active_n`-worth to the
    blend, same as a real computation would. When participants IS
    non-empty, non-participants still correctly get nothing here (they
    stay out of the returned dict) — that's their true, real lack of
    activity, not a case this fallback should touch.
    """
    participants = {uid: rate for uid, rate in rates.items() if rate}
    if not participants:
        _log.info(
            "population_relative_scores[%s]: no participants this window — "
            "pinning entire active roster (%d) to baseline %.1fx",
            label, len(active_uids), BASELINE_SCORE,
        )
        return {uid: BASELINE_SCORE for uid in active_uids}
    total_rate = sum(participants.values())
    if not total_rate:
        _log.debug("population_relative_scores[%s]: total_rate is zero — pinning to baseline", label)
        return {uid: BASELINE_SCORE for uid in participants}
    scores = {
        uid: round(active_n * rate / total_rate, 5)
        for uid, rate in participants.items()
    }
    _log.debug(
        "population_relative_scores[%s]: %d participants, active_n=%d, "
        "scores range %.2fx-%.2fx",
        label, len(participants), active_n,
        min(scores.values()), max(scores.values()),
    )
    return scores


def _share_complement_scores(
    values: dict[str, float], active_n: int, active_uids: Iterable[str], label: str = "metric",
) -> dict[str, float]:
    """{uid: value} -> {uid: score} for a "lower is better" raw average
    (e.g. cost per story, time per story) — Efficiency's alternative to
    `_population_relative_scores` (see module docstring for why these two
    aren't the same mechanism).

    `share_i = value_i / sum(all values)` — always in [0, 1] since it's one
    person's slice of a positive total that includes them. `score = (1 -
    share_i) * active_n / (n - 1)`, where `n` is how many people
    contributed a value here. Shares sum to 1 over those `n` people, so
    `sum(1-share) = n-1` — multiplying by `active_n/(n-1)` (not `n/(n-1)`,
    fixed 2026-07-08) rescales that to exactly `active_n`, matching
    `_population_relative_scores`'s per-metric total. An earlier version
    used `n/(n-1)`, which summed to `n` instead of `active_n` — harmless
    on its own, but it meant Efficiency's blend (see
    `_compute_efficiency_scores`) couldn't land on a universal total when
    its two metrics had different participant counts.

    No sparsity floor (removed 2026-07-08, see module docstring) — `1 -
    share` is bounded `[0, 1]` regardless of how few people participate,
    so a thin sample can't produce an unbounded or negative result.

    Two genuinely undefined cases (fixed 2026-07-08 to pin the FULL active
    roster to baseline, not just whoever showed up in `values`): zero
    participants, and `n=1` (`active_n/(n-1)` divides by zero — there's no
    comparison to make with only one data point, not a trust judgment).
    Confirmed on real deployed data: with `active_n=2` and only one
    person having any cost/time data, pinning just that one lone
    contributor left the *other* active person defaulting to 0% at the
    outer blend, summing to `100%` instead of the correct `200%`. Pinning
    the whole roster (both people) to baseline instead makes this metric
    contribute its full `active_n`-worth to the blend, same as a real
    computation would — see `_population_relative_scores`'s analogous
    zero-participants fix for the same reasoning.
    """
    participants = {uid: v for uid, v in values.items() if v}
    if not participants:
        _log.info(
            "share_complement_scores[%s]: no participants this window — "
            "pinning entire active roster (%d) to baseline %.1fx",
            label, len(active_uids), BASELINE_SCORE,
        )
        return {uid: BASELINE_SCORE for uid in active_uids}
    n = len(participants)
    if n == 1:
        _log.info(
            "share_complement_scores[%s]: single contributor — active_n/(n-1) "
            "undefined, pinning entire active roster (%d) to baseline %.1fx",
            label, len(active_uids), BASELINE_SCORE,
        )
        return {uid: BASELINE_SCORE for uid in active_uids}
    total = sum(participants.values())
    if not total:
        _log.debug("share_complement_scores[%s]: total is zero — pinning to baseline", label)
        return {uid: BASELINE_SCORE for uid in participants}
    correction = active_n / (n - 1)
    scores = {
        uid: round((1 - value / total) * correction, 5)
        for uid, value in participants.items()
    }
    _log.debug(
        "share_complement_scores[%s]: %d participants, active_n=%d, "
        "scores range %.2fx-%.2fx",
        label, n, active_n, min(scores.values()), max(scores.values()),
    )
    return scores


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


async def _fetch_goal_weightages(pool) -> dict[str, float]:
    """{goal_id: weightage} for every active goal — the per-goal redistribution
    weight in the shared formula. Defaults to 1.0 (see migrations/
    goal_weightage.sql) until the LLM-based holistic recompute sets real
    values."""
    rows = await pool.fetch("SELECT id, weightage FROM goals WHERE status = 'active'")
    return {row["id"]: float(row["weightage"]) for row in rows}


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
