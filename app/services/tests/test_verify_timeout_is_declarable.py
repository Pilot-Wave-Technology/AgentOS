"""A slow suite must be declarable, and must not read as a broken image.

WHAT HAPPENED WITHOUT THIS. Every timeout in the verifier was a
hardcoded 300 seconds and no caller overrode it. A real repository's
suite runs longer, so it timed out; a timeout is `error` -- rightly, a
check that did not finish did not say the work is wrong -- three errors
spend the adjudicator's strikes; and the executor was handed a message
telling it to fix its manifest, `.agentos/setup.sh` and env setup. It
would edit setup files it had no problem with, re-propose, time out
again, and fail with "N legs without converging" and no recorded reason.

The ERROR classification is deliberately unchanged. What was missing was
any way to SAY the suite needs longer, and any hint in the message that
the environment was not the problem.
"""

from __future__ import annotations

import pytest

from kernel.verifier import budget
from kernel.verifier.outcome import ERROR, from_exit


def test_the_default_is_unchanged_when_nobody_declares():
    assert budget.timeout_for({}, {}) == budget.DEFAULT_S
    assert budget.timeout_for(None, None) == budget.DEFAULT_S


def test_a_criterion_can_declare_its_own_budget():
    got = budget.timeout_for({}, {"spec": {"timeout_s": 1800}})
    assert got == 1800


def test_a_contract_can_declare_one_for_every_check():
    got = budget.timeout_for({"verify_timeout_s": 900}, {"spec": {}})
    assert got == 900


def test_the_criterion_wins_over_the_contract():
    got = budget.timeout_for({"verify_timeout_s": 900},
                             {"spec": {"timeout_s": 60}})
    assert got == 60, "the more specific declaration should win"


def test_it_is_capped():
    """A criterion holds an admission slot and a verifier pod for its
    whole run, and the contract is authored by the same model whose work
    is being checked -- so 'the contract says so' is not authority here."""
    assert budget.timeout_for({}, {"spec": {"timeout_s": 10 ** 9}}) \
        == budget.MAX_S


def test_it_has_a_floor():
    """Far more likely to be milliseconds mistaken for seconds than an
    intention -- a container cannot start in 0.2s."""
    assert budget.timeout_for({}, {"spec": {"timeout_s": 0.2}}) \
        == budget.MIN_S


def test_nonsense_falls_back_rather_than_raising():
    """A bad number in a contract must not make the story unverifiable."""
    assert budget.timeout_for({}, {"spec": {"timeout_s": "soon"}}) \
        == budget.DEFAULT_S


def test_a_timeout_is_still_an_error_not_a_fail():
    """Unchanged on purpose. A check that did not finish did not say the
    work is wrong, and wrongly refusing a story is worse than wrongly not
    refusing one -- a refusal cannot be argued with."""
    out = from_exit(124, "...", "pytest", timed_out=True)
    assert out.passes == ERROR


def test_the_default_timeout_says_it_is_not_an_environment_fault():
    """The message a model reads. Without this it goes and edits setup
    files for a suite that was merely slow."""
    out = from_exit(124, "...", "pytest", timed_out=True,
                    timeout_s=300.0, timeout_declared=False)
    reason = out.detail["reason"]
    assert "NOT an environment fault" in reason
    assert "verify_timeout_s" in reason, (
        "the message does not name the field to set, so the next run has "
        "no way to act on it")
    assert out.detail["timeout_s"] == 300.0


def test_a_declared_timeout_says_something_different():
    """Here the budget WAS chosen, so the useful reading is that the work
    is slower than expected or the check does not terminate."""
    out = from_exit(124, "...", "pytest", timed_out=True,
                    timeout_s=1800.0, timeout_declared=True)
    reason = out.detail["reason"]
    assert "declared" in reason
    assert "NOT an environment fault" not in reason
    assert out.detail["timeout_s"] == 1800.0


@pytest.mark.parametrize("contract,criterion,expected", [
    ({}, {}, False),
    ({"verify_timeout_s": 60}, {}, True),
    ({}, {"spec": {"timeout_s": 60}}, True),
])
def test_was_declared_reports_who_chose(contract, criterion, expected):
    assert budget.was_declared(contract, criterion) is expected


def test_verify_resolves_the_budget_per_criterion():
    """It used to pass one `timeout` down for the whole claim, so a
    per-criterion declaration could not have taken effect."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/verifier/verify.py", encoding="utf-8").read()
    assert "timeout_for(contract, criterion)" in src, (
        "the criterion's own budget is not resolved at the dispatch point")


# ── who chose the budget decides the verdict ─────────────────────────────

def test_a_default_timeout_is_an_error_because_nobody_chose_it():
    """300s is what happens when no one says. Exceeding it means nothing
    was checked, and a check that did not finish did not say the work is
    wrong -- so it must not refuse the claim."""
    from kernel.verifier.outcome import ERROR, from_exit

    out = from_exit(exit_code=None, output="", command="pytest",
                    timed_out=True, timeout_s=300.0, timeout_declared=False)
    assert out.detail["reason"]
    assert out.detail["timeout_declared"] is False
    # NOTE `Outcome.passes` holds the verdict WORD, not a boolean -- so
    # `not out.passes` is False for "error" and reads as a pass. Compare
    # against the constant.
    assert _status(out) == ERROR


def test_a_declared_timeout_is_a_fail_because_the_contract_promised_it():
    """`verify_timeout_s` is a claim that this check finishes in that
    long. Exceeding it is a fact about the WORK, not the machine.

    Both were ERROR before, which made the declaration unenforceable: a
    contract could ask for the two-hour ceiling, blow it, and spend the
    adjudicator's strikes on what reads as an environment fault. A budget
    nothing enforces is a comment.
    """
    from kernel.verifier.outcome import FAIL, from_exit

    out = from_exit(exit_code=None, output="", command="pytest",
                    timed_out=True, timeout_s=7200.0, timeout_declared=True)
    assert out.detail["timeout_declared"] is True
    assert _status(out) == FAIL


def _status(outcome):
    """The verdict word, whatever the dataclass calls its first field."""
    import dataclasses

    return getattr(outcome, dataclasses.fields(outcome)[0].name)
