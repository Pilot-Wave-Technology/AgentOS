"""Taximeter metric registry — v5.

Single source of truth for which per-human activity signals feed the
Taximeter view and which category they belong to. Mirrors the pattern in
`app/resource_kinds/__init__.py`: one dataclass, one dict, no DB-side metric
definitions. Adding a metric = one `MetricSpec` here + a `UNION ALL` branch
in a mat-view migration producing rows with that `metric_key` (the current
mat view, `dashboard_mv_taximeter_priority_days_weight.sql`'s
`mv_taximeter_human_activity`, already produces several metric_keys this
registry no longer reads — see "Removed in v5" below; their rows are
harmless, just unread, per the "Removing a metric" pattern this module has
always followed).

**v5 replaces v4's points-per-occurrence model entirely** — every metric
here now feeds a shared, population-relative formula (Oversight/Throughput)
or a separate ratio formula (Efficiency), both in `scoring.py`. There is no
more `points`/`point_buckets`/`rate` per metric — a metric's raw value is
used directly as the formula's input, not multiplied by a fixed weight.
Full history: v1 percentile -> v2 raw numbers -> v3 points -> v4 overall
score (all in `research/TAXIMETER_DESIGN.md`) -> v5 population-relative
(`D:\\Projects\\agent-control-panel\\TAXIMETER_V5_REDESIGN.md`).

**Removed in v5** (verified against real production data — see the redesign
doc's Oversight table for the full evidence per metric):
  - `decisions_made` — fires autonomously from an SHCA tool call, zero
    human involvement at the moment it fires.
  - `task_review_actions`, `questions_answered` — 0% happen in GO's own
    thread; both happen via assistant-chat, which Throughput/Efficiency
    already credit through story completion time/cost.
  - `escalations_resolved` — same as above, plus the escalation-to-GO
    mechanism never writes to the ledger at all.
  - `go_thread_interactions`, `story_thread_interactions` — never shipped
    as a metric in v4 either; `go_thread_interactions`'s anti-spam design
    was finalized but the metric itself stays held back by explicit
    choice.
  - Impact category entirely — its only real metric (weighted output) was
    already Throughput's in v4; nothing else in Impact ever shipped.

**Kept, unchanged in shape**:
  - `goal_management_actions` — Oversight's original survivor. Healthy,
    human-attributed, no GO-thread requirement needed (goal-level steering
    counts regardless of chat/non-chat).
  - `story_assignment_requests` — stays permanently inert (GO never sends
    `requested_by_uid`); shipped anyway per the "safe to ship inert"
    precedent, in case that ever changes.
  - `stories_completed`, `weighted_output_completed` — Throughput's volume
    signals, unchanged.
  - `story_completion_time`, `cost_per_story` — Efficiency's two ratios,
    same underlying data, new formula (see scoring.py).

**Added 2026-07-08**: `go_thread_interactions` — Oversight's second real
metric, human messages in GO's thread, but only counting a message when a
real GO-driven outcome follows within a short window (see this dict's
entry below and `dashboard_mv_taximeter_go_thread_antispam.sql` for the
exact event list and window). Activating this required also fixing
Oversight's aggregation from a plain sum to a 50/50 blend
(`_compute_oversight_scores` in scoring.py) — see that module's docstring
for why a plain sum breaks once a category has two real metrics.

**Not yet added** (verified reliable, held back by explicit user choice —
see redesign doc): story acceptance (`story.state_changed` action=`accept`,
100% actor_uid attribution confirmed 2026-07-08). Add a `MetricSpec` here
when that changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["oversight", "throughput", "efficiency"]


@dataclass(frozen=True)
class MetricSpec:
    """One row in the Taximeter's metric taxonomy.

    `key` must match the `metric_key` produced in the mat view.
    `label`/`description` are documentation only (no UI currently renders
    per-metric detail — the dashboard's "How is this calculated?" panel is
    a plain 3-sentence category summary by design; keep these here anyway
    since they're what a future reader needs to know what each metric
    actually counts).

    All three categories are hardcoded by name in their own dedicated
    scoring functions (`_compute_oversight_scores`,
    `_compute_throughput_scores`, `_compute_efficiency_scores`) rather
    than read generically by category — each blends its real metrics
    50/50 rather than summing them, since each metric's score already
    sums to N on its own and a plain sum of two such metrics would sum to
    2N (fixed 2026-07-08 for Throughput, then Oversight once
    `go_thread_interactions` activated a second real Oversight metric —
    see scoring.py's module docstring for the full story). `story_
    assignment_requests` stays out of Oversight's blend while it's
    permanently inert — see its own description below. Entries here exist
    for documentation/registry completeness, not as live inputs to a
    generic per-category loop (there isn't one anymore).
    """
    key: str
    category: Category
    label: str
    description: str


TAXIMETER_METRICS: dict[str, MetricSpec] = {
    # ── Oversight ────────────────────────────────────────────────────────
    "goal_management_actions": MetricSpec(
        key="goal_management_actions",
        category="oversight",
        label="Goal & team management",
        description="Creating goals and managing team membership.",
    ),
    "story_assignment_requests": MetricSpec(
        key="story_assignment_requests",
        category="oversight",
        label="Story assignment requests",
        description=(
            "Explicitly directing who works on a story. Currently inert — "
            "GO does not yet report when it's acting on an explicit human "
            "request; will activate automatically once it does."
        ),
    ),
    "go_thread_interactions": MetricSpec(
        key="go_thread_interactions",
        category="oversight",
        label="Goal thread interactions",
        description=(
            "Messages in GO's thread that led to GO actually doing "
            "something — creating or changing a story, creating or "
            "reassigning a task — within 4 minutes. A raw message count "
            "would reward filler ('ok', 'sounds good') as much as real "
            "steering, so only messages followed by a real GO-driven "
            "outcome count; see dashboard_mv_taximeter_go_thread_antispam.sql "
            "for the exact event list and window."
        ),
    ),

    # ── Throughput (volume of output) ───────────────────────────────────
    "stories_completed": MetricSpec(
        key="stories_completed",
        category="throughput",
        label="Stories completed",
        description="Number of stories finished.",
    ),
    "weighted_output_completed": MetricSpec(
        key="weighted_output_completed",
        category="throughput",
        label="Weighted output",
        description=(
            "Completed stories weighted by priority and days taken — a "
            "size proxy that needs no dedicated sizing field or GO "
            "tool/prompt support to populate."
        ),
    ),

    # ── Efficiency — see MetricSpec docstring: hardcoded by name in ───────
    # scoring.py's _compute_efficiency_scores, not read generically. The
    # mat view produces these as sum/count pairs (`{key}_sum`/`{key}_count`)
    # since averaging weekly averages isn't the same as a true window
    # average — see dashboard_mv_taximeter_priority_days_weight.sql.
    "story_completion_time": MetricSpec(
        key="story_completion_time",
        category="efficiency",
        label="Story completion time",
        description="Average time from starting to finishing a story.",
    ),
    "cost_per_story": MetricSpec(
        key="cost_per_story",
        category="efficiency",
        label="Cost efficiency",
        description=(
            "Average dollar cost per story — rewards effective model "
            "choice (getting the work done well without defaulting to the "
            "most expensive model), not just raw token volume."
        ),
    ),
}
