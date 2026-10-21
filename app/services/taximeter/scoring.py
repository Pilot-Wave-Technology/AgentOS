"""Taximeter activity engine — the names the rest of the app imports.

THE MODULE WAS 960 LINES AND IS NOW SEVEN UNDER 200. Nothing here
computes anything: this file exists so `dashboard_metrics.py` and the
dashboard routers keep importing one stable place while the engine
underneath is split by question --

    reads.py           what the scorer reads, and the two filters every
                       read shares (active goals only; currently-scored
                       metric keys only)
    normalise.py       the population-relative maths, pure
    window.py          active weeks, and N for each scope
    goals.py           which goals count, their weights, who works in them
    metric_scores.py   one metric, scored across a population
    category_scores.py Oversight, Throughput, Efficiency
    compute.py         the population, the window, the overall
    drilldown.py       one person's score, broken down by goal

The design history and worked examples that used to live at the top of
this file are unchanged and now sit with the code they describe.
"""
from __future__ import annotations

from app.services.taximeter.compute import compute_activity
from app.services.taximeter.drilldown import compute_goal_drilldown
from app.services.taximeter.goals import (
    fetch_goal_list_with_contributors,
    fetch_taximeter_goals,
)
from app.services.taximeter.reads import SORTABLE_COLUMNS

__all__ = [
    "compute_activity",
    "compute_goal_drilldown",
    "fetch_goal_list_with_contributors",
    "fetch_taximeter_goals",
    "SORTABLE_COLUMNS",
]
