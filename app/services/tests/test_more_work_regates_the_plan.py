"""Sending a run back for more work re-gates it, errand or story.

WHAT WAS WATCHED ON ERRAND 187. A person was shown the acceptance card
-- "accept as done, or describe what more this story should do" -- and
described more. The executor did that work and proposed completion
again without writing a plan. Its questions, in order:

    authorization   the first plan, approved
    direction
    acceptance      answered with MORE WORK
    permission
    acceptance      the second claim, no plan between them

THE RULE WAS ALREADY THERE AND ITS TEST WAS TOO NARROW.
`plan_is_approved` ends with "a verdict spends the approval that
preceded it", whose own comment describes this exact failure: "a story
a person sent back for MORE WORK started that work immediately, with no
plan and nothing to approve". It looked for the spending in one place:

    SELECT 1 FROM verdicts WHERE execution_id = $1 AND created_at > $2

AN ERRAND IS NEVER VERIFIED. Zero verdicts across every errand this
environment has run. So `spent` was always false, the first approval
stood for the life of the run, and the re-gate that makes a story
replan could not fire for an errand at all.

What ends a cycle is the CLAIM BEING ANSWERED -- by a verifier or by a
person -- not which of the two happened to run. For a story the
acceptance is answered at the same moment the verdict lands, so nothing
about the story path changes; for an errand it is the only signal there
is.
"""
from __future__ import annotations

import inspect

import pytest

from kernel.harness import park, park_approval


def _sql() -> str:
    """The body of the plan gate, comments stripped.

    Read what RUNS: this file's own prose says the word `verdicts` many
    times, and so does the source's explanation of why verdicts are not
    the only signal.

    BOTH FUNCTIONS, because the rule moved and the rule is what this
    file guards. `plan_is_approved` used to hold these queries inline;
    they now live in `_cycle_closed`, because the STANDING GRANT check
    has to consult them too. That check returned early, in front of both
    rules, so a grant-backed errand ran every cycle after the first
    under cycle one's approval -- errand 282 did exactly that. Reading
    only the outer function would let the queries be deleted outright
    and still pass.
    """
    src = (inspect.getsource(park.plan_is_approved)
           + "\n"
           + inspect.getsource(park_approval._cycle_closed))
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_the_grant_cannot_outrank_a_closed_cycle():
    """The ordering bug itself, guarded.

    A standing grant authorises unattended RUNNING; it must not
    resurrect a cycle a person has closed. If the grant check runs
    before the cycle is known, the spend below it is unreachable for
    every errand that has one -- which is every autonomous errand.
    """
    src = inspect.getsource(park.plan_is_approved)
    body = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "_cycle_closed" in body, (
        "the gate must work out whether the cycle is closed")
    assert body.index("_cycle_closed") < body.index("standing_approval"), (
        "the cycle has to be known BEFORE the grant is honoured; a grant "
        "checked first returns True and nothing below it ever runs")


def test_an_answered_acceptance_spends_the_approval():
    """THE FIX. Without it an errand's approval never expires."""
    sql = _sql()
    from kernel.harness.cycles import CYCLE_ENDING_KINDS
    assert "kind = ANY($3::text[])" in sql and "CYCLE_ENDING_KINDS" in sql
    assert "acceptance" in CYCLE_ENDING_KINDS, (
        "only a verdict spends the approval, and an errand never "
        "produces one -- so a person asking for more work gets the work "
        "done under the plan that was just declared finished")
    assert "landing" in CYCLE_ENDING_KINDS, (
        "a story holding its workspace finishes on a landing park; its "
        "feature plan would never be spent")
    assert "'ANSWERED'" in sql


def test_the_verdict_path_still_spends_it():
    # The story path must be untouched: a refused verification re-gates
    # the run without anyone answering anything.
    assert "FROM verdicts" in _sql()


def test_both_are_one_question_not_two_code_paths():
    """One statement, so a story and an errand cannot diverge.

    Two queries with an `if kind == 'errand'` between them is how the
    two substrates drifted everywhere else in this system; the union
    keeps "has this cycle been closed" a single fact.
    """
    sql = _sql()
    assert "UNION ALL" in sql
    assert "errand" not in sql.lower(), (
        "nothing here may branch on the kind of run")


def test_it_is_scoped_to_this_approval():
    # `> $2` is answered_at of the approval being tested. Without the
    # bound, an acceptance answered on an EARLIER cycle would spend an
    # approval granted after it, and the run could never write again.
    sql = _sql()
    assert "answered_at > $2" in sql


@pytest.mark.asyncio
async def test_a_run_with_no_acceptance_yet_keeps_its_approval():
    """The ordinary case: approved, working, nothing answered since.

    If this regressed, every run would be re-gated immediately after
    approval and could never make its first edit.
    """
    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"status": "ANSWERED", "declined": False,
                    "verdict": "approve", "answered_at": "2026-09-16",
                    "execution_id": 187}

        async def fetchval(self, sql, *args):
            # no replan, and nothing has spent it
            return None

    assert await park.plan_is_approved(_Conn(), 187) is True
