"""The park wake must agree with who is actually allowed to approve.

MEASURED, NOT ARGUED. Run 306 on 2026-09-24:

    13:41:49  [ERRAND PARKED] "This needs a person and you cannot settle it"
    13:42:03  GO reports the plan to the person, exactly as told
    13:45:19  the progress wake: "approve or send back the plan"
    13:45:29  GO approves, ten seconds later

Nothing was slow. GO obeyed the first wake and reversed when the second
contradicted it, and the 3m40s in between was the wall-clock bucket
boundary falling where it did -- it could as easily have been fourteen
minutes. The authority to approve an autonomous errand's plan moved in
four places and this fifth copy of the old rule was the one GO read
first.

WHICH IS WHY THE TEST IS ON THE TEXT. The permission is enforced in the
kernel and was correct throughout; what failed was a sentence telling
the agent not to use a power it had.
"""

from __future__ import annotations

import io
import pathlib
import re

from app.services.go_wake_events import _parked_wake

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = io.open(ROOT / "app/services/go_wake_events.py", encoding="utf-8").read()


def _q(**kw) -> dict:
    base = {"execution": 306, "question": 300, "kind": "authorization",
            "question_text": "plan approval", "go_driven": True,
            "proposed_by_me": False}
    base.update(kw)
    return base


# -- the text ---------------------------------------------------------

def test_a_plan_on_an_autonomous_errand_is_gos_to_settle():
    out = _parked_wake(_q())
    assert "YOURS to settle" in out
    assert "cannot settle" not in out, (
        "the wake still carries the old rule, which is what GO reads "
        "first and obeys")


def test_it_names_the_tool_and_the_argument_that_makes_it_gos():
    """A wake that grants a power without naming the call spends a turn
    on GO looking the call up -- and `decide_plan` is GO'S decision only
    when `human_uid` is LEFT OUT, which nothing else in the message
    would tell it."""
    out = _parked_wake(_q())
    assert "decide_plan" in out
    assert "question_id=300" in out
    assert "human_uid" in out


def test_a_plan_without_a_grant_still_belongs_to_a_person():
    """The power is autonomous errands only. A hand-started errand
    parks for a person exactly as before."""
    out = _parked_wake(_q(go_driven=False))
    assert "cannot settle" in out
    assert "decide_plan" not in out


def test_the_other_human_only_kinds_are_untouched():
    """`acceptance` is the one the person answered on run 306 and must
    keep answering: done-or-not-done is the decision this whole feature
    deliberately leaves with them."""
    for kind in ("acceptance", "escalation", "direction", "permission"):
        out = _parked_wake(_q(kind=kind))
        assert "cannot settle" in out, kind
        assert "YOURS to settle" not in out, kind


def test_a_clarification_is_still_settleable_and_not_a_plan():
    out = _parked_wake(_q(kind="clarification", go_driven=False))
    assert "relay_answer" in out and "decide_plan" not in out


def test_it_still_says_when_to_fetch_a_person_anyway():
    """Power without a boundary is the failure mode this replaces. The
    four cases are the ones a person approving an unattended errand
    would not have meant to delegate."""
    out = _parked_wake(_q())
    for phrase in ("money", "production", "irreversible"):
        assert phrase in out, phrase


def test_gos_decision_is_explained_on_the_thread():
    """Nobody else has seen the plan. A decision with no account of it
    is the autonomy people object to, and correctly."""
    assert "report back to" in _parked_wake(_q())


# -- the query behind it ----------------------------------------------

def _sql() -> str:
    body = SRC[SRC.index("async def on_execution_parked("):]
    return body[body.index('"""'):body.index("if row is None")]


def test_a_goal_wide_grant_counts_as_autonomous():
    """THE FIRST CUT JOINED `s.story_id = e.story_id`, so a grant
    covering every errand in the goal -- `story_id IS NULL`, the
    broadest and least-attended kind -- matched nothing, `go_driven`
    came back false, and GO was sent to find a person for a plan it
    owned. The canonical predicate has always had the NULL arm."""
    assert "s.story_id IS NULL OR s.story_id = e.story_id" in _sql()


def test_it_carries_every_clause_of_the_canonical_grant():
    """Two definitions of "autonomous" disagree the first time one
    moves, and the disagreement is GO acting where it may not."""
    for clause in ("approved_by IS NOT NULL", "revoked_at IS NULL",
                   "expires_at IS NULL OR s.expires_at > now()"):
        assert clause in _sql(), clause


def test_no_column_references_a_join_that_is_not_there():
    """THE ALIAS TRAP. Turning the LEFT JOIN into an EXISTS scopes `s`
    inside the subquery; a leftover `s.proposed_by` in the select list
    is then an undefined alias -- the query fails to PARSE, so no park
    wake is sent at all and the feature looks switched off rather than
    broken."""
    # COMMENTS EXPLAIN THE TRAP and name the alias while doing it, so
    # they have to go before the alias is looked for.
    sql = " ".join(l for l in _sql().splitlines()
                   if not l.strip().startswith("--"))
    outer = re.sub(r"EXISTS \(.*?\) AS go_driven", "", sql, flags=re.S)
    outer = re.sub(r"\(SELECT p\..*?\) AS proposed_by", "", outer, flags=re.S)
    assert "s." not in outer, (
        "a column still reads from the removed grant join: " + outer)


# ── the seam between the query and the renderer ──────────────────────

def test_every_key_the_renderer_reads_is_actually_passed():
    """RUN 307, AND THE MOST INSTRUCTIVE FAILURE OF THE SET.

    The query selected `go_driven`. `_parked_wake` read
    `q.get("go_driven")`. Nothing put it in the dict between them, so
    `decidable` was always falsey and the branch could not fire once --
    while both ends passed their tests, because one test called
    `_parked_wake` with the flag supplied by hand and the other read the
    SQL. The join was the only untested part and the only broken one.

    This test is written against the seam rather than either end: every
    key the renderer reads must appear in the literal the caller builds.
    It would have caught `go_driven`, and it catches the next one."""
    reads = set(re.findall(r'q\.get\("([a-z_]+)"\)', SRC))
    reads |= set(re.findall(r'q\["([a-z_]+)"\]', SRC))
    call = SRC[SRC.index("_parked_wake({"):]
    call = call[:call.index("}")]
    passed = set(re.findall(r'"([a-z_]+)":', call))
    missing = reads - passed
    assert not missing, (
        f"_parked_wake reads {sorted(missing)} and the caller never "
        f"passes them, so those branches can never fire")


def test_the_flag_comes_from_the_row_and_is_not_assumed():
    """Passing a constant would be the same bug wearing a value."""
    call = SRC[SRC.index("_parked_wake({"):]
    call = call[:call.index("}")]
    assert '"go_driven": bool(row["go_driven"])' in call


# ── the plan has to be IN the wake ───────────────────────────────────

def test_the_wake_carries_the_plan_and_not_just_its_label():
    """RUN 308. The whole body of the wake was the two words "plan
    approval" -- `question_text` -- while the five-step plan sat in
    `q.payload`, rendered only onto a card GO never reads. GO approved a
    plan it had not been shown, which is not a decision, and it makes
    the entire autonomy feature hollow rather than merely slow."""
    out = _parked_wake(_q(
        plan_text="1. Inspect main.py  2. Write the report"))
    assert "Inspect main.py" in out
    assert "YOURS to settle" in out


def test_the_plan_leads_and_the_label_is_the_fallback():
    """`question_text` on an authorization is the word "plan approval",
    which repeats the header and says nothing. It is still what a park
    with no plan must fall back to -- a wake with no body at all would
    be worse."""
    withplan = _parked_wake(_q(plan_text="STEP ONE"))
    # The evidence sits above the imperative in the shared shape,
    # so the plan is read before the instruction that acts on it.
    assert withplan.index("STEP ONE") < withplan.index("decide_plan")
    without = _parked_wake(_q(plan_text=""))
    assert "plan approval" in without


def test_the_query_reads_the_payload_the_plan_lives_in():
    body = SRC[SRC.index("async def on_execution_parked("):]
    assert "q.payload" in body[:body.index("if row is None")]


def test_the_plan_is_read_with_the_cards_own_helpers():
    """Two renderings of one plan disagree the first time either moves,
    and the disagreement is GO approving a description of work that is
    not the work."""
    fn = SRC[SRC.index("def _plan_text("):]
    fn = fn[:fn.index("def _parked_wake(")]
    assert "from kernel.assistant.cards import as_text, plan_of" in fn


def test_an_escalation_payload_is_not_read_as_a_plan():
    """It carries `checks` too, meaning "the checks that keep
    refusing" rather than "the checks that define done". The card
    learned this the hard way; the wake reads the same guard instead of
    rediscovering it."""
    fn = SRC[SRC.index("def _plan_text("):]
    fn = fn[:fn.index("def _parked_wake(")]
    assert "HUMAN_ANSWERED_KINDS" in fn


def test_a_broken_payload_does_not_kill_the_park_wake():
    """A wake with no plan is worse than one with; a wake that raises is
    worse than both, because the park then produces nothing and the
    errand looks abandoned."""
    fn = SRC[SRC.index("def _plan_text("):]
    fn = fn[:fn.index("def _parked_wake(")]
    assert "except Exception" in fn and 'return ""' in fn
