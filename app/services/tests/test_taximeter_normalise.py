"""The population-relative maths, exhaustively.

SCORE = N x YOUR RATE / EVERYONE'S RATE is the one claim the whole
taximeter rests on: every metric, category and overall must sum to N, so
that 1.0 means exactly a fair share and the columns can be compared with
each other. These functions take numbers and return numbers -- no pool,
no clock -- so the claim can be checked over random populations rather
than over a fixture somebody chose.

THE FIXES THESE PIN ARE REAL ONES, dated in `normalise.py`'s own
comments: the zero-participant case used to return `{}` and let a blend
0.0-fill it, which capped a category at half of N; and the share
complement used to rescale by `n/(n-1)` instead of `active_n/(n-1)`, so
Efficiency could not land on a universal total when its two metrics had
different participant counts.

S0 IS A PURE REFACTOR, so this covers the maths as it stands. The v2
clamp and the efficiency pool do not exist yet and are tested in S4,
beside the code that introduces them -- a test written here first would
have nothing to fail against.
"""
from __future__ import annotations

import random

from app.services.taximeter.categories import BASELINE_SCORE
from app.services.taximeter.normalise import (
    _population_relative_scores,
    _share_complement_scores,
)

# WHY NOT 1e-9. Both functions `round(..., 5)` before returning, so the
# sum of n scores can be off by up to n x 5e-6. That is invisible at the
# two decimals the dashboard shows, but it means "sums to N" is exact to
# the rounding and no further -- a test asserting 1e-9 would fail for
# ever, and the build plan asked for one. The tolerance is derived from
# the rounding rather than guessed, so tightening the rounding tightens
# the test automatically.
ROUNDING = 1e-5


def tol(n: int) -> float:
    return n * ROUNDING


def _uids(n: int) -> list[str]:
    return [f"u{i}" for i in range(n)]


# ── score = N x rate / total rate ───────────────────────────────────

def test_scores_sum_to_N_over_a_thousand_random_populations():
    """The invariant, not an example of it."""
    rng = random.Random(20261015)
    for _ in range(1000):
        n = rng.randint(1, 12)
        uids = _uids(n)
        # Some people active, some not: a real window has both.
        rates = {u: (rng.random() * rng.choice([0, 1, 10, 1000])) for u in uids}
        scores = _population_relative_scores(rates, n, uids)
        if not any(rates.values()):
            continue                      # the no-participant case, below
        assert abs(sum(scores.values()) - n) < tol(n), (rates, scores)


def test_a_non_participant_scores_nothing():
    """Not baseline, and not absent-and-then-filled: zero activity is a
    real answer, and the fallback below must not reach for it."""
    scores = _population_relative_scores({"a": 4.0, "b": 0.0}, 2, ["a", "b"])
    assert scores.get("b", 0.0) == 0.0
    assert abs(scores["a"] - 2.0) < tol(2)


def test_nobody_participated_pins_the_WHOLE_roster_to_baseline():
    """Returning `{}` here is what capped a category at half of N: a
    blend 0.0-filled the missing metric, so the category could never
    contribute more than its other half."""
    uids = ["a", "b", "c"]
    scores = _population_relative_scores({}, 3, uids)
    assert scores == {u: BASELINE_SCORE for u in uids}
    assert abs(sum(scores.values()) - 3) < tol(3)

    # All-zero rates is the same situation written differently.
    assert _population_relative_scores({u: 0.0 for u in uids}, 3, uids) == scores


def test_one_person_scores_exactly_one():
    assert _population_relative_scores({"a": 7.0}, 1, ["a"]) == {"a": 1.0}


def test_an_outlier_takes_a_large_share_but_never_more_than_N():
    """The formula is linear and bounded at N, which is why the sparsity
    floor could be removed. A 483x outlier is the real shape seen in
    production, and it must not produce something unbounded."""
    uids = _uids(5)
    rates = {u: 1.0 for u in uids}
    rates["u0"] = 483.0
    scores = _population_relative_scores(rates, 5, uids)
    assert abs(sum(scores.values()) - 5) < tol(5)
    assert scores["u0"] < 5.0
    assert scores["u0"] > 4.0           # it should dominate, just not break


def test_rates_are_scale_free():
    """Multiplying everyone's rate changes no score — which is what lets
    goal weights be relative without anybody normalising them first."""
    uids = _uids(4)
    rates = {u: i + 1.0 for i, u in enumerate(uids)}
    a = _population_relative_scores(rates, 4, uids)
    b = _population_relative_scores({u: v * 37.5 for u, v in rates.items()}, 4, uids)
    for u in uids:
        assert abs(a[u] - b[u]) < 1e-5


# ── the "lower is better" complement ────────────────────────────────

def test_the_share_complement_also_sums_to_N():
    """`active_n/(n-1)`, NOT `n/(n-1)`. The old rescaling summed to the
    participant count, so Efficiency's blend could not reach a universal
    total when its two metrics had different participant counts."""
    uids = _uids(6)
    values = {"u0": 1.0, "u1": 3.0, "u2": 6.0}      # 3 of 6 people have a cost
    scores = _share_complement_scores(values, 6, uids)
    assert abs(sum(scores.values()) - 6) < tol(6)


def test_lower_is_better_in_the_complement():
    scores = _share_complement_scores({"cheap": 1.0, "dear": 9.0}, 2, ["cheap", "dear"])
    assert scores["cheap"] > scores["dear"]


def test_a_single_contributor_cannot_be_ranked_against_nobody():
    """`active_n/(n-1)` is undefined at n=1. Pinning the roster to
    baseline is the honest answer: one person's cost is not evidence
    about anybody's efficiency."""
    uids = ["a", "b"]
    assert _share_complement_scores({"a": 5.0}, 2, uids) == {u: BASELINE_SCORE for u in uids}


def test_no_contributors_pins_the_roster_too():
    uids = ["a", "b", "c"]
    assert _share_complement_scores({}, 3, uids) == {u: BASELINE_SCORE for u in uids}


def test_the_rounding_is_what_bounds_the_sum():
    """Stated outright, because the tolerance above depends on it: a
    score is rounded to 5 decimals, so N scores can miss N by N x 5e-6.
    If the rounding ever changes, this fails and the tolerance above is
    revisited rather than quietly widened."""
    scores = _population_relative_scores({"a": 1.0, "b": 2.0, "c": 4.0}, 3, ["a", "b", "c"])
    for v in scores.values():
        assert round(v, 5) == v, f"{v} carries more than 5 decimals"
