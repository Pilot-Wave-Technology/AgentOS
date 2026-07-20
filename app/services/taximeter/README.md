# app/services/taximeter/

Activity engine for the Taximeter dashboard panel — per-human scores built
from real ledger/story data. See `research/TAXIMETER_DESIGN.md` for the
v1-v4 history (percentile -> raw numbers -> points -> overall score) and
`D:\Projects\agent-control-panel\TAXIMETER_V5_REDESIGN.md` for the full v5
redesign discussion this module now implements.

## Files

| File | Purpose |
|---|---|
| `metrics.py` | `TAXIMETER_METRICS` — the metric registry. Source of truth for which signals exist and which category they belong to. |
| `categories.py` | `CATEGORY_ORDER` / `CATEGORY_LABELS` — the Oversight/Throughput/Efficiency grouping. `CATEGORY_WEIGHTS` — Oversight 30 / Throughput 50 / Efficiency 20, combined into `overall`. `BASELINE_SCORE` — the shared formula's neutral fallback for mathematically undefined cases. |
| `scoring.py` | `compute_activity(window_weeks, goal_id, sort, direction)` — one row per user with an `overall` score and a population-relative score per category, sorted by `sort`/`direction`; powers both the People view and the Goals view's per-goal drill-in (same function, `goal_id` set or not). `fetch_goal_list_with_contributors(window_weeks)` — the Goals tab's landing list. `compute_goal_drilldown(uid, window_weeks)` — the per-person modal's one-row-per-goal table. |

## v5: population-relative scoring, not points-per-occurrence

Every prior version (see `research/TAXIMETER_DESIGN.md`) assigned each
metric occurrence a fixed point value and summed them. v5 replaces this
entirely: **score = N × individual_rate / total_rate**, where N is the
count of everyone platform-active in the window. Baseline = **1.0** — "your
fair share of the total work." A score of 2.0x means twice the average;
0.5x means half.

**Oversight and Throughput** each run their two real metrics through the
same per-metric formula (`_compute_metric_population_scores`), then blend
the two results 50/50 (`_compute_oversight_scores`, `_compute_throughput_scores`
— NOT a plain sum, see below):
1. Per goal, redistribute that goal's `weightage` (a new `goals` column,
   see Goal weightage below) among its contributors, proportional to each
   person's share of that goal's raw metric total. A goal's weight is
   never exceeded, only redistributed.
2. Sum a person's goal-weighted contributions across all their goals.
3. Divide by the person's own **active-week** count (not the fixed window
   length) to get a rate — corrects for tenure, same rationale as v4.
4. Run the population-relative ratio on that rate.

When scoped to a single goal (the dashboard's goal picker), step 1's
redistribution is skipped — there's only one goal's population to
consider, so the ratio runs directly on that goal's own raw values.

**Oversight's metrics**: `goal_management_actions` (goal creation, team
membership) and `go_thread_interactions` (added 2026-07-08 — human
messages in GO's thread, but only counted when a real GO-driven outcome
follows within 4 minutes; see `dashboard_mv_taximeter_go_thread_antispam.sql`
for the exact outcome-event list and why a raw message count would reward
filler). `story_assignment_requests` stays permanently inert (GO never
reports acting on an explicit request) and is excluded from the blend —
see `_compute_oversight_scores`'s docstring for why including a
permanently-zero third term would just dilute the two real signals with
baseline noise.

**Efficiency** is NOT goal-weighted (time/cost isn't a shared pool to
redistribute the way a count is) and combines differently: two "lower is
better" raw averages — time per story, cost per story — each run through
`_share_complement_scores`, blended 50/50. That function computes each
person's bounded *share* of the team total (`value / sum(values)`, always
in `[0, 1]`) and scores `(1 - share) * active_n/(n-1)`, where `n` is
however many people contributed a value (fixed 2026-07-08: an earlier
version used `n/(n-1)`, which summed to `n` instead of `active_n` — see
"Universal total" below). This is a different mechanism than
Oversight/Throughput's population-relative ratio — an earlier attempt
instead inverted the raw metric to a points-per-unit rate and ran that
through the *same* ratio machinery, then (for a version before that)
tried flipping the ratio's output directly (`1/p`, then `1-p`); both
broke on real data (`1/p` gave a 483x outlier for a low-sample
contributor, `1-p` goes negative once the input ratio exceeds 1). Working
off a genuine bounded share instead of an unbounded already-normalized
ratio avoids both failure modes by construction — see the redesign doc's
Efficiency section for the worked examples.

## Universal total: every category sums to exactly active_n (2026-07-08)

All three categories — Oversight, Throughput, Efficiency — sum to exactly
`active_n` across their participants, at both platform and goal scope,
regardless of how much the two metrics feeding a blend overlap in who
participates. Two things had to be true together for this to hold
unconditionally, not just when the data happened to cooperate:

1. **Every per-metric formula sums to exactly `active_n` on its own.**
   `_population_relative_scores` always did. `_share_complement_scores`
   needed its correction factor changed from `n/(n-1)` to `active_n/(n-1)`
   to match (see above).
2. **Every blend fills a missing metric with `0.0`, not `BASELINE_SCORE`.**
   Filling with `1.0` looked harmless but silently broke the total
   whenever a category's two metrics had different participants: each
   "partial participant" (in one metric but not the other) injected an
   extra `+0.5` of unaccounted-for credit. Confirmed on real deployed
   data — Oversight summed to `3.5×active_n`-worth (350% for 3 people)
   instead of `3.0×` before this fix, from exactly one person who did
   `goal_management_actions` without `go_thread_interactions` activity
   that window. Throughput never showed this because its two metrics
   (`stories_completed`, `weighted_output_completed`) are both derived
   from the same `story_completions` rows in the mat view, so their
   participants always match by construction — the bug was latent there
   too, just never observable. With a `0.0` fill, every blend decomposes
   cleanly: `0.5 × (metric A's own total) + 0.5 × (metric B's own total)
   = active_n`, independent of participant overlap.
3. **A metric that can't compute a real ratio at all pins the FULL active
   roster to baseline, not just whoever showed up with data.** Zero
   participants, or `_share_complement_scores`'s `n=1` (its correction
   factor divides by zero — no comparison is possible with one data
   point) both count as "genuinely can't measure this," and previously
   returned baseline only for the lone/found participant(s), leaving
   everyone else to default to `0.0` at the blend. Confirmed on real
   deployed data at goal scope: `active_n=2`, only one person had cost/time
   data, and pinning just that one person summed to `100%` instead of the
   correct `200%`. Both scoring functions now take an `active_uids`
   parameter (`active_weeks.keys()`, already computed for tenure
   adjustment) so they can pin every active person, not just the ones
   they happened to find data for.

**No sparsity floor** (removed 2026-07-08). A `SPARSITY_FLOOR_RATIO` guard
lived here through most of v5's build — pinning everyone to
`BASELINE_SCORE` whenever fewer than half of the active population showed
any activity on a metric — but it existed specifically to guard against a
now-eliminated failure mode (the 483x reciprocal-flip outlier above), and
never actually fired on real production data in the time it existed. Both
formulas currently in use are bounded by construction (see their
docstrings), so the floor was removed rather than kept unused. One
narrower, unrelated guard remains: `_share_complement_scores` still pins
to baseline when only one person has a value that window, because its
`n/(n-1)` correction is mathematically undefined at `n=1` — that's a
divide-by-zero guard, not a trust judgment.

## Goal weightage

New `goals.weightage` column (`migrations/goal_weightage.sql`) — how much a
goal's activity counts toward Oversight/Throughput scoring, relative to
other goals. Open-ended, no fixed ceiling (a genuinely large goal can just
have a large number). Defaults to **1.0** for every goal, making every
goal count equally until real values are set.

**Not yet built**: the LLM-based holistic recompute that sets real
weightage values (Gemini Flash, triggered by a background watchdog on the
`agent-management` service calling the `task-runner` Cloud Run Job, which
polls for goals whose story/resource count has grown since
`weightage_recomputed_at`). Until that ships, every goal stays at the
default 1.0, which is a deliberately neutral starting point — the scoring
formula works correctly today and gets more accurate once real weightage
values start flowing in, not blocked on that pipeline existing first.

## Removed in v5 (verified against real production data)

See the redesign doc's Oversight table for full evidence. Summary:
`decisions_made` (fires autonomously, zero human involvement),
`task_review_actions`/`questions_answered` (0% happen in GO's own thread),
`escalations_resolved` (same, plus the escalation mechanism never writes
to the ledger), `go_thread_interactions`/`story_thread_interactions`
(never shipped as a metric; the former's anti-spam design is finalized but
held back by explicit choice), and the entire Impact category (its one
real metric was already Throughput's).

**Not yet added**: story acceptance — verified 100% reliable
(`story.state_changed` action=`accept`), held back by explicit user
choice, not a data problem.

## Active-goals-only filter

Only activity tied to a goal with `status = 'active'` counts toward
anyone's numbers (`_ACTIVE_GOAL_ONLY` in `scoring.py`), applied to every
query that reads `mv_taximeter_human_activity`. `goal_id IS NULL` rows
pass through unaffected — that's a different case from "tied to an
inactive goal."

This is checked against the **live** `goals.status`, not a snapshot taken
when the activity happened — so a person's numbers can shift on their own
if a goal they were working on later gets paused, completed, cancelled, or
deleted, even with no new activity of their own. That's intentional: the
Taximeter is meant to reflect what's contributing to someone's *current*
picture, not a permanent historical ledger.

## Per-person drill-down modal

Clicking a name opens `_taximeter_goal_breakdown.html`, fed by
`fetch_taximeter_goal_breakdown()` in `dashboard_metrics.py` →
`compute_goal_drilldown()`: one row per goal the person is active in, with
that goal's own Oversight/Throughput/Efficiency/Overall scores — computed
the same way the goal picker's goal-scoped view computes them, so the
numbers are directly comparable across goals. Replaces v4's separate
point-math table + bar chart with a single table (layout resolved
2026-07-08).

**Goal-scope-aware**: when the main table itself is filtered to one goal,
the drill-down narrows to just that one row, matching what's on screen.

## Goal scoping

`compute_activity(window_weeks, goal_id)` optionally scopes every score
computation to one goal via the fact view's `goal_id` column — the
dashboard's goal picker re-runs the same computation filtered to a single
goal instead of platform-wide. `fetch_taximeter_goals()` lists goals that
have any recorded activity, for the picker's option list.

## People / Goals views

**Currently People-view only in the UI** — the Goals tab toggle was pulled
from `system_taximeter.html` ("added noise without adding signal for
now"), but the backend stayed intact (`view=goals` route branch,
`fetch_goal_list_with_contributors`) in case it's revisited.

**An earlier version got this wrong.** It aggregated everyone active in a
goal into one summed total per goal (`compute_activity_by_goal()`, since
deleted). Feedback: "this in itself doesn't mean anything as we are just
trying to understand people's performance" — a single blended number per
goal answers a different question than the one asked. The actual ask was
"for any given goal you can see how people contributed and what
capacity" — i.e. Goals should be an *entry point* into the same per-person
view, not a new metric. Fixed by routing the Goals tab's drill-in through
`compute_activity()` directly, and this hasn't changed in v5.

## Adding a metric

1. Add a `UNION ALL` branch to a mat-view migration producing rows shaped
   `(uid, goal_id, metric_key, week_start, value)` for your new
   `metric_key`. This requires a *new* migration file, never an edit to an
   already-deployed one (see this repo's migration immutability rule in
   `CLAUDE.md`).
2. Add one `MetricSpec` to `TAXIMETER_METRICS` in `metrics.py`, under
   whichever category it belongs to. If it's Oversight or Throughput, it's
   automatically picked up by `scoring.py`'s generic per-metric loop — no
   other code change needed. If it's Efficiency, its formula is hardcoded
   in `_compute_efficiency_scores` (see that function's docstring for
   why) — adding a third Efficiency metric means extending that function
   by hand, not just adding a registry entry.
3. Deploy — `REFRESH MATERIALIZED VIEW` picks up the new rows, and
   `scoring.py` picks up the new registry entry automatically for
   Oversight/Throughput metrics.

## Removing a metric

Delete its `UNION ALL` branch and its `MetricSpec` entry. Old rows for the
retired `metric_key` are simply never read again — no cleanup migration
needed.
