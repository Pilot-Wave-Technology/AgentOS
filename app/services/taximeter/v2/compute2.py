"""The v2 score: three categories, each summing to N, blended into one.

    overall = 0.30 Oversight + 0.50 Throughput + 0.20 Efficiency

EVERY COLUMN SUMS TO N, so 1.0 is exactly a fair share and the columns
can be read against each other. That is the whole reason for the
population-relative form; it is reused from v1 unchanged.

WHAT v2 CHANGES is what goes into it:

  Oversight   decisions answered in YOUR stories (whoever answered)
              plus stewardship, instead of v1's raw event counts
  Throughput  items and priority points, instead of story completions
  Efficiency  priority points per HOUR -- a rate, where v1 used a
              "lower is better" complement of raw cost. Cost is
              reported beside the scores and not scored: a VS Code
              story records no tokens, so its cost is unknown rather
              than zero, and scoring an unknown rewards or penalises
              the lane rather than the work.

  goal weight `1 + items completed in the goal`, computed live,
              instead of a number an LLM wrote on the goal.

AND THE WINDOW IS EACH PERSON'S OWN, as in v1: a rate is a total over
the weeks that person was actually active, so joining late does not
read as working less.
"""

from __future__ import annotations

import logging

from app.services.taximeter.categories import CATEGORY_WEIGHTS
from app.services.taximeter.normalise import _population_relative_scores
from app.services.taximeter.v2 import reads2
from app.services.taximeter.v2.goal_weight import goal_weights, weight_for
from app.services.taximeter.v2.normalise2 import clamp_to_median_band, pool_scores

_log = logging.getLogger(__name__)


def _rate(totals: dict[str, float], weeks: dict[str, int]) -> dict[str, float]:
    """Total over the person's OWN active weeks. Never over zero."""
    return {uid: v / max(weeks.get(uid, 1), 1) for uid, v in totals.items()}


def _goal_weighted_rate(by_goal: dict[tuple[str, str], float],
                        weights: dict[str, float],
                        weeks: dict[str, int]) -> dict[str, float]:
    """Each goal's contribution, scaled by that goal's weight.

    A DECISION IN A BUSY GOAL COUNTS FOR MORE, which is the one piece
    of judgement in the formula and the thing the LLM weightage was
    trying to express. `1 + items` makes it evidence rather than
    opinion.
    """
    out: dict[str, float] = {}
    for (uid, gid), value in by_goal.items():
        out[uid] = out.get(uid, 0.0) + value * weight_for(weights, gid)
    return _rate(out, weeks)


async def compute_v2(pool, window_weeks: int = 12,
                     goal_id: str | None = None) -> dict[str, dict]:
    """`{uid: {overall, oversight, throughput, efficiency, detail}}`."""
    everyone = await reads2.people(pool, window_weeks, goal_id)
    if not everyone:
        return {}
    n = len(everyone)
    weeks = await reads2.active_weeks(pool, window_weeks, goal_id)
    # AT GOAL SCOPE THERE IS ONE GOAL, so weighting its contributions
    # against each other is meaningless -- every weight would be the
    # same number and cancel. v1 skips the redistribution there for the
    # same reason.
    weights = ({} if goal_id else await goal_weights(pool, window_weeks))

    # ── Oversight: 75% decisions in your stories, 25% stewardship ──
    decisions = await reads2.points_by_person_and_goal(
        pool, window_weeks, "decision:", goal_id)
    steward = await reads2.points_by_person_and_goal(
        pool, window_weeks, "steward:", goal_id)
    s_dec = _population_relative_scores(
        _goal_weighted_rate(decisions, weights, weeks), n, everyone, "decisions")
    s_stw = _population_relative_scores(
        _goal_weighted_rate(steward, weights, weeks), n, everyone, "steward")
    oversight = {u: 0.75 * s_dec.get(u, 0.0) + 0.25 * s_stw.get(u, 0.0)
                 for u in everyone}

    # ── Throughput: 50% items, 50% priority points ──
    items_g = await reads2.totals_by_person_and_goal(
        pool, window_weeks, "items", goal_id)
    points_g = await reads2.totals_by_person_and_goal(
        pool, window_weeks, "priority_points", goal_id)
    s_items = _population_relative_scores(
        _goal_weighted_rate(items_g, weights, weeks), n, everyone, "items")
    s_points = _population_relative_scores(
        _goal_weighted_rate(points_g, weights, weeks), n, everyone, "points")
    throughput = {u: 0.5 * s_items.get(u, 0.0) + 0.5 * s_points.get(u, 0.0)
                  for u in everyone}

    # ── Efficiency: points per dollar, points per hour ──
    # THE POOL IS WHO FINISHED SOMETHING. Efficiency is a question
    # about delivered work; somebody who delivered none is not
    # inefficient, they are not in the conversation.
    #
    # COST IS REPORTED, NOT SCORED, and that is a deliberate reversal.
    # Scoring points-per-dollar meant scoring people on a dimension
    # that only exists for some of them: a story worked in VS Code
    # records NO tokens at all -- measured on feature, 0 local stories
    # have a `token_calls` row against 51 cloud ones -- so its cost is
    # not zero, it is unknown.
    #
    # Every treatment of an unknown is a reward or a penalty relative
    # to being measured. Giving the unmeasured the pool average made
    # working locally an insurance policy against a below-average cost
    # score: never top, never bottom, no exposure. Weak, bounded at
    # 10% of Overall, and pointing the wrong way.
    #
    # So Efficiency measures the one thing that is true of everybody:
    # priority points delivered per hour from start to finish. Cost
    # stays in the view and rides along in `detail` as a NUMBER on the
    # page, because "what did this cost" is a real question -- it is
    # just not the same question as "how is this person doing", and
    # answering both with one column is what created the distortion.
    items = await reads2.totals_by_person(pool, window_weeks, "items", goal_id)
    pool_uids = {u for u, v in items.items() if v > 0}
    cost = await reads2.totals_by_person(pool, window_weeks, "cost_usd", goal_id)
    hours = await reads2.totals_by_person(pool, window_weeks, "cycle_hours_sum", goal_id)
    all_pts = await reads2.totals_by_person(pool, window_weeks, "priority_points", goal_id)

    per_hour = {u: all_pts[u] / hours[u]
                for u in pool_uids if hours.get(u, 0) > 0 and all_pts.get(u)}
    # The clamp still earns its place: a story finished in four minutes
    # should not take the whole column. The pool average still applies
    # to anybody who finished work but has no measurable duration.
    efficiency = pool_scores(clamp_to_median_band(per_hour), pool_uids, n)

    out: dict[str, dict] = {}
    for uid in everyone:
        cats = {"oversight": round(oversight.get(uid, 0.0), 5),
                "throughput": round(throughput.get(uid, 0.0), 5),
                "efficiency": round(efficiency.get(uid, 0.0), 5)}
        out[uid] = {
            **cats,
            "overall": round(sum(CATEGORY_WEIGHTS[c] * v for c, v in cats.items()), 5),
            # FOR THE EXPLAINER: raw -> rate -> score, per metric, so the
            # dashboard can show the chain rather than restate the rules.
            "detail": {
                "active_weeks": weeks.get(uid, 1),
                "items": items.get(uid, 0.0),
                "priority_points": all_pts.get(uid, 0.0),
                "cost_usd": cost.get(uid, 0.0),
                "cycle_hours": hours.get(uid, 0.0),
                "points_per_hour": per_hour.get(uid),
                "in_efficiency_pool": uid in pool_uids,
            },
        }
    _log.debug("taximeter v2: scored %d people, goal=%s, window=%dw",
               n, goal_id or "*", window_weeks)
    return out
