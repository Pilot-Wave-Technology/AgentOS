"""The cost panel's own logic: formatting, the reconcile flag, and the page.

Separate from `test_cost_attribution_sql.py`, which checks the SQL against a
real database. These need none and so run everywhere -- which matters most
for the reconcile flag, because the case worth testing is the one a healthy
database cannot produce.
"""
from __future__ import annotations

import asyncio

from app.services.cost_attribution import SPENDERS, money, spend_by_role


def test_sub_cent_spend_never_renders_as_zero():
    """$0.00 against real spend reads as free, and free is the one wrong
    answer nobody questions. Small numbers get four places instead."""
    assert money(0.0004) == "$0.0004"
    assert money(0.0099) == "$0.0099"
    assert money(0) == "$0.00"          # actually nothing, and says so
    assert money(None) == "$0.00"
    assert money(12.5) == "$12.50"
    assert money(1234.5) == "$1,234.50"


class _FakePool:
    """One canned answer, shaped like `v_cost_reconciliation`."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return self._rows


def _row(**kw):
    base = {f"{s}_cost": 0.0 for s in SPENDERS}
    base.update({"goal_id": "G1", "total_cost": 0.0, "attributed_cost": 0.0,
                 "unattributed_cost": 0.0, "unpriced_calls": 0,
                 "unpriced_tokens": 0})
    base.update(kw)
    return base


def _spend(rows, monkeypatch):
    import app.services.cost_attribution as mod

    monkeypatch.setattr(mod, "get_pool", lambda: _FakePool(rows))
    return asyncio.run(spend_by_role())


def test_a_balanced_ledger_reconciles(monkeypatch):
    got = _spend([_row(total_cost=10.0, attributed_cost=8.0,
                       unattributed_cost=2.0, assistant_cost=3.0,
                       executor_cost=6.0, verifier_cost=1.0)], monkeypatch)
    assert got["reconciles"] is True
    assert got["total_str"] == "$10.00"
    assert [r["role"] for r in got["roles"]] == ["assistant", "executor", "verifier"]
    # Ordered by the taxonomy, not by size: the columns must line up between
    # rows, and a legend that reorders itself per row cannot be scanned.
    assert got["roles"][0]["pct"] == 30.0


def test_an_unbalanced_ledger_is_caught_and_says_so(monkeypatch):
    """THE CASE A HEALTHY DATABASE CANNOT PRODUCE, and the reason this test
    exists at all.

    If a new producer ever writes a row with a spender nobody classified,
    the parts stop summing to the total. Silently, and on a page people go
    on quoting. The flag is what turns that into a red line on the panel
    rather than a discrepancy noticed months later.
    """
    got = _spend([_row(total_cost=10.0, attributed_cost=10.0,
                       assistant_cost=3.0, executor_cost=4.0)], monkeypatch)
    assert got["reconciles"] is False


def test_unattributed_spend_still_counts_toward_the_total(monkeypatch):
    """GO spend belongs to no conversation and must not therefore vanish.
    It is reported on its own line, not dropped and not split across
    threads — splitting would invent a number."""
    got = _spend([_row(total_cost=5.0, attributed_cost=2.0,
                       unattributed_cost=3.0, orchestrator_cost=3.0,
                       assistant_cost=2.0)], monkeypatch)
    assert got["reconciles"] is True
    assert got["unattributed_str"] == "$3.00"


def test_unpriced_calls_are_surfaced_not_swallowed(monkeypatch):
    got = _spend([_row(total_cost=1.0, attributed_cost=1.0,
                       assistant_cost=1.0, unpriced_calls=18,
                       unpriced_tokens=1475)], monkeypatch)
    assert got["unpriced_calls"] == 18
    assert got["unpriced_tokens"] == 1475


def test_a_broken_query_degrades_instead_of_breaking_the_dashboard(monkeypatch):
    """A cost panel is a report. It must not be able to take the page down
    — the views it reads are new, and a rollback that removed them should
    cost a panel, not the dashboard."""
    import app.services.cost_attribution as mod

    class _Boom:
        async def fetch(self, *_a, **_k):
            raise RuntimeError("relation does not exist")

    monkeypatch.setattr(mod, "get_pool", lambda: _Boom())
    got = asyncio.run(spend_by_role())
    assert got["available"] is False and got["roles"] == []


# ── the page ─────────────────────────────────────────────────────────────

def _render(ctx):
    import pathlib

    from jinja2 import Environment, FileSystemLoader

    root = pathlib.Path(__file__).resolve().parents[3] / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(root)))
    return env.get_template(
        "dashboard/partials/system_cost_attribution.html").render(c=ctx)


def test_the_panel_renders_and_shows_every_spender():
    html = _render({
        "by_role": {
            "available": True, "total": 10.0, "total_str": "$10.00",
            "attributed": 8.0, "unattributed": 2.0, "unattributed_str": "$2.00",
            "reconciles": True, "unpriced_calls": 0, "unpriced_tokens": 0,
            "roles": [{"role": "assistant", "cost": 3.0, "cost_str": "$3.00", "pct": 30.0},
                      {"role": "executor", "cost": 7.0, "cost_str": "$7.00", "pct": 70.0}],
        },
        "threads": [{
            "thread_id": "t1", "goal_id": "KER20", "story_id": "abc123",
            "title": "Implement modulo", "cost": 1.8, "cost_str": "$1.80",
            "tokens": 100, "cached": 5305898, "unpriced": 0,
            "parts": [{"role": "assistant", "cost": 0.4, "cost_str": "$0.40"},
                      {"role": "executor", "cost": 1.4, "cost_str": "$1.40"}],
        }],
    })
    assert "Where the money goes" in html
    assert "assistant" in html and "executor" in html
    assert "Implement modulo" in html and "abc123" in html
    assert "5,305,898" in html, "cached tokens must be shown, they are the story"
    assert "does not reconcile" not in html


def test_the_panel_shouts_when_it_does_not_reconcile():
    html = _render({
        "by_role": {"available": True, "total": 10.0, "total_str": "$10.00",
                    "attributed": 7.0, "unattributed": 0.0,
                    "unattributed_str": "$0.00", "reconciles": False,
                    "unpriced_calls": 0, "unpriced_tokens": 0,
                    "roles": [{"role": "assistant", "cost": 7.0,
                               "cost_str": "$7.00", "pct": 70.0}]},
        "threads": [],
    })
    assert "does not reconcile" in html


def test_the_panel_disappears_rather_than_erroring_with_no_data():
    """Before the migrations land, or on a fresh database, there is nothing
    to attribute. An empty panel with headings is worse than no panel."""
    assert _render({"by_role": {"available": False, "roles": []},
                    "threads": []}).strip() == ""
    assert _render({"by_role": None, "threads": []}).strip() == ""
