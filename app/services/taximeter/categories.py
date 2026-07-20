"""Taximeter category display order, weights, and shared-formula constants
— v5.

Redesigned from v4's four equal-weighted categories (Oversight/Impact/
Throughput/Efficiency, 25% each) down to three, unequally weighted. Impact
is dropped entirely — its one real metric (weighted output) already lives
under Throughput; the other (`go_thread_interactions`) never shipped, see
metrics.py. Full design history: `research/TAXIMETER_DESIGN.md` (v1-v4) and
`D:\\Projects\\agent-control-panel\\TAXIMETER_V5_REDESIGN.md` (v5).

`CATEGORY_WEIGHTS` combine into `overall` — unlike v4's plain average, this
is now an explicit weighted sum: Throughput (50%) counts most, Oversight
(30%) next, Efficiency (20%) least (rebalanced 2026-07-08 from an initial
40/40/20 split).

**No sparsity floor** (removed 2026-07-08 — see `scoring.py`'s module
docstring for the full reasoning). A `SPARSITY_FLOOR_RATIO` constant lived
here through most of v5's design and initial build: it pinned everyone to
`BASELINE_SCORE` whenever fewer than half of the active population showed
any activity on a metric. It existed to guard against a specific,
now-eliminated failure mode (a reciprocal-flip formula producing a 483x
outlier on real data for a low-sample contributor — see the redesign doc).
Both formulas now in use (`_population_relative_scores`,
`_share_complement_scores`) are bounded by construction and can't
reproduce that failure, so the floor was removed rather than kept as an
unused check — it never actually fired on real production data in the
time it existed. Re-add a similar guard if a future formula change
introduces an unbounded transform again.
"""
from __future__ import annotations

CATEGORY_ORDER: list[str] = ["oversight", "throughput", "efficiency"]

CATEGORY_WEIGHTS: dict[str, float] = {
    "oversight": 0.30,
    "throughput": 0.50,
    "efficiency": 0.20,
}

CATEGORY_LABELS: dict[str, str] = {
    "oversight": "Oversight",
    "throughput": "Throughput",
    "efficiency": "Efficiency",
}

# The population-relative baseline: "your fair share of the total work" —
# returned in place of a real ratio when a formula hits a mathematically
# undefined case (e.g. a single contributor, or a zero total), and the
# natural center of the ratio distribution otherwise.
BASELINE_SCORE: float = 1.0
