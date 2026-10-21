"""The population-relative maths. No database, no clock, no logging.

PURE, AND THAT IS THE POINT OF SEPARATING IT. `score = N * rate / total
rate` is the one thing in the taximeter that must be provably correct --
every metric, category and overall has to sum to N -- and a function
that only takes numbers can be tested exhaustively over random
populations without a fixture in sight.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.services.taximeter.categories import BASELINE_SCORE

_log = logging.getLogger(__name__)


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
