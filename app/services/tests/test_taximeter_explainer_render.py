"""The dashboard text says what the code does, and cannot drift from it.

THE FAILURE THIS PREVENTS IS A QUIET ONE. The explainer used to state
the weights and the decision points as prose, so changing a weight
meant remembering to edit a paragraph -- and the version nobody
remembered to edit is the one people read. It told everyone a goal's
weight was "set by an LLM" for as long as that was true and then for
however long it was not.

SO THE NUMBERS ARE RENDERED, and these tests assert each rendered
value equals its constant. Change a weight and this file fails, which
is the point: the test is not checking the text is correct today, it
is making the text impossible to leave behind.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PANEL = _ROOT / "app/templates/dashboard/partials/system_taximeter.html"
_BREAKDOWN = _ROOT / "app/templates/dashboard/partials/_taximeter_goal_breakdown.html"


def _panel() -> str:
    return _PANEL.read_text(encoding="utf-8")


def _rendered() -> str:
    """The panel with its scoring values substituted, as Jinja would.

    Rendered rather than pattern-matched on the template source: a test
    that only greps the source cannot tell a number that is rendered
    from one that is typed, which is the whole distinction here.
    """
    from jinja2 import Environment

    from app.services.dashboard_metrics import _scoring_constants

    body = _panel()
    # The panel is a fragment of a larger page; render just the parts
    # that depend on `tx.scoring` by giving Jinja exactly that.
    env = Environment(autoescape=False)
    # The column headers call the page's `sort_th` macro, which does not
    # exist outside the full template. Stubbed to return its arguments:
    # what is being checked is the NUMBERS that were substituted into
    # them, not the markup the macro would wrap around them.
    env.globals["sort_th"] = lambda *a, **k: " ".join(str(x) for x in a)
    out = []
    for m in re.finditer(r"\{\{[^}]*tx\.scoring[^}]*\}\}", body):
        out.append(env.from_string(m.group(0)).render(
            tx={"scoring": _scoring_constants()}))
    for m in re.finditer(r"\{%\s*for[^%]*tx\.scoring.*?\{%\s*endfor\s*%\}", body, re.S):
        out.append(env.from_string(m.group(0)).render(
            tx={"scoring": _scoring_constants()}))
    return "\n".join(out)


# ── the numbers come from the constants ─────────────────────────────

def test_the_text_FOLLOWS_the_weights_rather_than_agreeing_with_them():
    """CHANGE THE CONSTANT, THE TEXT MUST CHANGE.

    The obvious version of this test renders from the constant and then
    looks for the constant, so both sides move together and it can
    never fail -- it was written that way first, and passed happily
    with the weights altered. Proving "rendered, not typed" means
    altering the source of truth and checking the output moved.
    """
    import app.services.taximeter.categories as cats

    before = _rendered()
    original = dict(cats.CATEGORY_WEIGHTS)
    try:
        cats.CATEGORY_WEIGHTS.update(
            {"oversight": 0.35, "throughput": 0.45, "efficiency": 0.20})
        after = _rendered()
    finally:
        cats.CATEGORY_WEIGHTS.clear()
        cats.CATEGORY_WEIGHTS.update(original)

    assert after != before, (
        "the explainer said the same thing with different weights — the "
        "numbers are typed into the panel, not rendered from the table")
    assert "35" in after and "45" in after


def test_the_decision_points_follow_the_table_too():
    """Same proof for the points: a kind added to the table appears in
    the panel without anybody editing the panel."""
    import app.services.taximeter.v2.points as pts

    original = dict(pts.DECISION_POINTS)
    try:
        pts.DECISION_POINTS["invented_for_this_test"] = 7
        rendered = _rendered()
    finally:
        pts.DECISION_POINTS.clear()
        pts.DECISION_POINTS.update(original)

    assert "invented_for_this_test 7" in rendered, (
        "the decision points are a hand-written list, not the table")


def test_every_decision_point_is_rendered():
    from app.services.taximeter.v2.points import DECISION_POINTS

    rendered = _rendered()
    for kind in DECISION_POINTS:
        assert kind in rendered, f"the explainer never mentions {kind}"


def test_every_priority_weight_is_rendered():
    from app.services.taximeter.v2.points import PRIORITY_POINTS

    rendered = _rendered()
    for name, pts in PRIORITY_POINTS.items():
        assert f"{name} {pts}" in rendered, f"priority {name} is not rendered"


def test_the_clamp_bounds_are_rendered():
    from app.services.taximeter.v2.normalise2 import CLAMP_HIGH, CLAMP_LOW

    rendered = _rendered()
    assert str(CLAMP_LOW) in rendered and str(CLAMP_HIGH) in rendered


def test_no_category_weight_is_TYPED_anywhere_in_the_panel():
    """THE PARTIAL CASE, which the render comparison cannot see.

    `_rendered()` extracts Jinja expressions, so a weight written as
    static text is invisible to it -- and one heading hardcoded while
    three others render still changes the output when a weight moves,
    so the comparison above passes. Checked by looking for the literal
    instead: any percentage in the prose that happens to equal a
    category weight is either typed, or a coincidence worth rewording.
    """
    from app.services.taximeter.categories import CATEGORY_WEIGHTS

    prose = re.sub(r"\{\{.*?\}\}", "", _panel(), flags=re.S)   # drop expressions
    prose = re.sub(r"\{%.*?%\}", "", prose, flags=re.S)        # and blocks
    prose = re.sub(r"\{#.*?#\}", "", prose, flags=re.S)        # and comments

    weights = {str(round(w * 100)) for w in CATEGORY_WEIGHTS.values()}
    typed = [m.group(0) for m in re.finditer(r"\b(\d{1,3})\s*%", prose)
             if m.group(1) in weights]
    assert not typed, (
        f"a category weight is typed into the panel: {typed}. It must be "
        "rendered from CATEGORY_WEIGHTS, or it will outlive the weight.")


# ── what must no longer be said ─────────────────────────────────────

def test_no_LLM_or_Weightage_text_remains():
    for path in (_PANEL, _BREAKDOWN):
        body = path.read_text(encoding="utf-8")
        prose = re.sub(r"\{#.*?#\}", "", body, flags=re.S)   # comments may explain
        assert "set by an LLM" not in prose, path.name
        assert "Weightage" not in prose, path.name


def test_the_breakdown_names_the_goal_weight_and_how_it_is_made():
    body = _BREAKDOWN.read_text(encoding="utf-8")
    assert "Goal weight" in body
    assert "work items finished" in body


# ── what must still be said ─────────────────────────────────────────

def test_the_ownership_rule_is_stated_plainly():
    """It is the rule everything else follows, and the one most likely
    to surprise somebody reading their own score."""
    body = _panel()
    assert "belongs to the person it is assigned to" in body
    assert "working for you" in body


def test_the_local_cost_case_is_explained():
    """R5. Cost counts only work that recorded spend, so somebody who
    works mostly in VS Code is scored on the few stories that did not.
    Unexplained, that looks arbitrary to exactly the people it affects
    most."""
    body = _panel()
    assert "no model spend" in body or "records no model spend" in body
    assert "local" in body.lower()


def test_the_sum_to_N_claim_is_stated():
    body = _panel()
    assert "add up to the number of people" in body


def test_the_percent_score_toggle_is_untouched():
    """The panel has two spellings of every score and swaps them with a
    class. Rewriting the copy must not have collapsed them."""
    body = _panel()
    assert "tx-mode-percent" in body and "tx-mode-score" in body


def test_a_deleted_goal_is_said_to_count_for_nothing():
    assert "Deleted and archived goals" in _panel()


# ── the numbers a label promises are the numbers it shows ───────────

def test_the_breakdown_renders_the_real_goal_weight_not_the_retired_column():
    """The header was renamed to "Goal weight" in S6 while the cell
    still rendered `goals.weightage` — the column the Gemini job used
    to write and nothing sets any more. It showed 1.00 for every goal
    under a label claiming it meant something, which is worse than the
    old honest label."""
    body = _BREAKDOWN.read_text(encoding="utf-8")
    cells = re.sub(r"\{#.*?#\}", "", body, flags=re.S)
    assert "g.goal_weight" in cells, "the cell does not render the real weight"


def test_the_efficiency_copy_does_not_promise_a_cost_score():
    """Cost is reported, not scored. Copy saying otherwise would make
    the Cost column look like part of the ranking it is deliberately
    kept out of."""
    body = _panel()
    assert "per dollar" not in body, "the copy still describes cost scoring"
    assert "not</em> scored" in body or "not scored" in body


def test_the_cost_column_is_reported_without_a_sort_link():
    """A sortable column reads as a ranking. This one is a fact."""
    body = _panel()
    assert ">Cost</th>" in body, "cost is not shown at all"
    assert "sort_th('cost" not in body, "cost is sortable — that reads as a score"


def test_every_scored_column_IS_sortable():
    """The other side of the same rule: a score you cannot sort by is
    a number the page is hiding."""
    body = _panel()
    for col in ("overall", "oversight", "throughput", "efficiency"):
        assert f"sort_th('{col}'" in body, f"{col} is not sortable"
