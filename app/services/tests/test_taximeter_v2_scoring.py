"""The v2 maths: the clamp, the efficiency pool, and the blend.

DEFERRED HERE FROM S0 ON PURPOSE. The build plan listed these under
the refactor stage, but the clamp and the pool are v2 concepts that did
not exist then -- a test written against them in S0 would have had
nothing to fail against, which is the failure mode of a test that
passes while proving nothing.

WHY A CLAMP AT ALL. v2 scores priority points PER DOLLAR and PER HOUR,
which is the right question and turns one cheap story into an
unbounded rate: a critical story delivered in ten minutes on a free
model is genuinely efficient, but not a thousand times more efficient
than everyone else. Unclamped, that person takes the whole of N. The
483x figure below is the real shape seen in production -- it is what
retired v4's reciprocal-flip formula.
"""
from __future__ import annotations

from app.services.taximeter.v2.goal_weight import weight_for
from app.services.taximeter.v2.normalise2 import (
    CLAMP_HIGH,
    CLAMP_LOW,
    clamp_to_median_band,
    pool_scores,
)
from app.services.taximeter.v2.points import DECISION_POINTS, decision_points

TOL = 1e-4


# ── the clamp ───────────────────────────────────────────────────────

def test_a_483x_outlier_is_pulled_to_four_times_the_median():
    rates = {"a": 1.0, "b": 1.0, "c": 1.0, "freak": 483.0}
    out = clamp_to_median_band(rates)
    assert out["freak"] == 4.0, out
    assert out["a"] == 1.0, "an ordinary rate was moved"


def test_the_band_scales_with_the_population():
    """A team that gets uniformly faster is not squashed: the band is
    relative to the median, not a fixed ceiling."""
    slow = clamp_to_median_band({"a": 1.0, "b": 1.0, "c": 100.0})
    fast = clamp_to_median_band({"a": 100.0, "b": 100.0, "c": 10000.0})
    assert slow["c"] / slow["a"] == fast["c"] / fast["a"]


def test_a_zero_is_not_clamped_UP():
    """Zero means "did not participate". Lifting it to a quarter of the
    median would pay somebody for work they did not do."""
    out = clamp_to_median_band({"a": 4.0, "b": 4.0, "idle": 0.0})
    assert out["idle"] == 0.0


def test_a_very_low_rate_is_pulled_up_to_the_floor():
    out = clamp_to_median_band({"a": 10.0, "b": 10.0, "slow": 0.01})
    assert out["slow"] == 10.0 * CLAMP_LOW


def test_one_participant_has_nobody_to_be_an_outlier_against():
    assert clamp_to_median_band({"a": 500.0}) == {"a": 500.0}


def test_the_band_bounds_are_a_quarter_and_four_times():
    """Stated outright: the copy in S6 renders these constants, and a
    change here must show up there rather than drift."""
    assert (CLAMP_LOW, CLAMP_HIGH) == (0.25, 4.0)


# ── the efficiency pool ─────────────────────────────────────────────

def test_the_pool_sums_to_N():
    scores = pool_scores({"a": 1.0, "b": 3.0}, {"a", "b"}, 2)
    assert abs(sum(scores.values()) - 2) < TOL


def test_somebody_outside_the_pool_scores_nothing():
    """Not because they were inefficient -- because efficiency is a
    question about delivered work and they delivered none."""
    scores = pool_scores({"a": 1.0, "b": 3.0}, {"a", "b"}, 3)
    assert "idle" not in scores


def test_a_local_only_person_scores_the_pool_average():
    """COST NOT APPLICABLE. A story worked entirely in VS Code records
    no spend, so its owner has no cost rate. Zero would punish them for
    the absence of a number; N/c is the honest "no evidence"."""
    scores = pool_scores({"paid": 2.0, "alsopaid": 2.0}, {"paid", "alsopaid", "local"}, 3)
    assert abs(scores["local"] - 3 / 3) < TOL
    assert abs(sum(scores.values()) - 3) < TOL


def test_with_fewer_than_two_measurable_rates_everyone_gets_the_average():
    """One person's cost is not evidence about anybody's efficiency --
    the same reason v1 pins a single contributor to baseline."""
    scores = pool_scores({"only": 7.0}, {"only", "other"}, 2)
    assert abs(scores["only"] - 1.0) < TOL
    assert abs(scores["other"] - 1.0) < TOL


def test_an_empty_pool_scores_nobody():
    assert pool_scores({}, set(), 4) == {}


def test_the_pool_still_sums_to_N_with_a_mix_of_measurable_and_not():
    pool = {"a", "b", "c", "d"}
    scores = pool_scores({"a": 1.0, "b": 9.0}, pool, 4)
    assert abs(sum(scores.values()) - 4) < TOL
    assert scores["b"] > scores["a"], "higher points-per-dollar must score higher"
    assert abs(scores["c"] - scores["d"]) < TOL, "both unmeasurable, both average"


# ── the points table ────────────────────────────────────────────────

def test_an_unknown_decision_kind_is_worth_nothing():
    """NOT a default of 1. A kind nobody has weighed is not evidence of
    effort, and defaulting would mean every new proposal kind the
    kernel invents quietly starts paying whoever owns the story."""
    assert decision_points("decision:invented_last_tuesday") == 0
    assert decision_points("steward:nothing_like_this") == 0
    assert decision_points("items") == 0


def test_machine_bookkeeping_is_absent_from_the_table():
    """An `answer` is already counted through its question; counting
    both would pay twice for one decision."""
    for kind in ("completion", "replan", "question", "answer"):
        assert kind not in DECISION_POINTS, kind


def test_the_committing_decisions_are_worth_most():
    """The shape of the scale, asserted rather than assumed: deciding
    the story's fate outranks unblocking it, which outranks steering a
    run already in flight."""
    assert decision_points("decision:authorization") == 3
    assert decision_points("decision:acceptance") == 3
    assert decision_points("decision:local_plan") == 3
    assert decision_points("decision:merge") == 2
    assert decision_points("decision:steer") == 1
    assert (decision_points("decision:authorization")
            > decision_points("decision:merge")
            > decision_points("decision:steer"))


def test_the_local_plan_decision_is_worth_the_same_as_a_pod_approval():
    """The same act, through a different door. Paying less for it would
    make the local lane look like less work than it is."""
    assert (decision_points("decision:local_plan")
            == decision_points("decision:authorization"))


# ── goal weight ─────────────────────────────────────────────────────

def test_a_goal_that_delivered_nothing_still_weighs_one():
    """Zero would erase every decision made in a goal that happened to
    finish nothing this period -- which is the period a goal is most
    likely to be hard, not least likely to matter."""
    assert weight_for({}, "quiet-goal") == 1.0
    assert weight_for({"busy": 9.0}, "quiet-goal") == 1.0


def test_a_busy_goal_weighs_more():
    assert weight_for({"busy": 9.0}, "busy") > weight_for({}, "quiet")
