"""F2 — what one criterion produced, in four values.

R1 settled the vocabulary and one rule with teeth: **a refusal is `fail`
only.** `flake` and `error` must never refuse, because a refusal
permanently blocks re-claiming — a story that was actually finished
would be killed by a rate limit, a dead network or a container that
would not start.

    pass    the criterion ran and was satisfied
    fail    the criterion ran and was NOT satisfied     ← the only refusal
    flake   the criterion disagrees with itself across runs (F3's quorum
            decides this; a single run never reports it)
    error   the criterion could not be run at all

The distinction between `fail` and `error` is the whole reason this is
four-valued rather than boolean. A two-valued verdict has to call a
container that failed to start "the work is wrong", and that is both
untrue and unrecoverable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PASS = "pass"
FAIL = "fail"
FLAKE = "flake"
ERROR = "error"

VALUES = (PASS, FAIL, FLAKE, ERROR)

# The only value that refuses a completion claim.
REFUSING = (FAIL,)


@dataclass(frozen=True)
class Outcome:
    """One run of one criterion."""

    passes: str
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.passes not in VALUES:
            raise ValueError(
                f"{self.passes!r} is not a criterion outcome; expected one "
                f"of {', '.join(VALUES)}")

    @property
    def refuses(self) -> bool:
        return self.passes in REFUSING


def from_exit(exit_code: int | None, output: str, command: str,
              timed_out: bool = False, timeout_s: float | None = None,
              timeout_declared: bool = False,
              infra_error: str | None = None) -> Outcome:
    """A command criterion's outcome, from what the container reported.

    NO EXIT CODE MEANS NO VERDICT, and that rule is enforced here rather
    than trusted to each driver. `infra_error` is how a substrate says
    the run was ended by something that is not the work — a Spot node
    reclaimed, an image that would not pull, a quota that refused the
    pod. Docker has no such case: a container exits or the API call
    raises. Kubernetes has several, and they are ordinary rather than
    exotic.

    Before this argument existed the Kubernetes driver had to express
    them as `exit_code=1`, which lands on the `fail` branch at the
    bottom of this function — THE WORK IS WRONG. A story would be
    refused because Google took a node back, and a refusal is the one
    verdict that cannot be argued with. It is `error` instead, which R1
    makes non-refusing, so the criterion is re-run and the parity report
    counts it as unrunnable.

    A missing exit code with NO explanation lands in the same place. A
    driver that returns None has told us nothing, and inventing a
    failing exit code to fill the hole is how the bug above happened.

    A TIMEOUT IS `error` WHEN NOBODY CHOSE THE BUDGET. A check that did
    not finish did not say the work is wrong — it said nothing. Calling
    that a refusal blocks re-claiming on a slow machine, which is the
    failure mode R1 exists to prevent.

    A DECLARED budget is the exception, and the reason is that somebody
    chose the number. `verify_timeout_s` on the contract, or `timeout_s`
    on a criterion's spec, is a claim that this check finishes in that
    long; exceeding it is a fact about the WORK, not about the machine,
    so it is a `fail`. Both cases were `error` until now, which made a
    declared budget unenforceable — a contract could ask for two hours,
    blow it, and spend the adjudicator's strikes on what reads to
    everyone as an environment fault. A budget nothing enforces is a
    comment.

    The asymmetry is deliberate and points the same way as everything
    else here: the DEFAULT stays non-refusing, because nobody asserted
    anything about it.

    Exit 124 is what the driver reports for a timeout and is also what
    `timeout(1)` conventionally returns, so the flag is authoritative
    rather than the code: a check that genuinely exits 124 on its own has
    failed, and must not be excused.

    EXIT 127 IS `error` TOO, for the same reason and found at G0. 127 is
    the POSIX shell's reserved "command not found", so a criterion
    returning it did not run — the image is missing the tool. Reported as
    `fail` it says THE WORK IS WRONG, which is the exact inversion F4's
    first pass produced from the other side: six attacks "failing closed"
    in an image with no pytest, having checked nothing at all.

    It also mattered more after F11: a check that can never run refuses
    every claim, so the story could never pass, and the refusal named the
    work rather than the image. Now it is an `error`, which R1 makes
    non-refusing — the claim is not blocked and the parity report has
    something to say instead.

    A program that genuinely exits 127 by choice is excused by this. That
    is the same trade the timeout makes, and in the same direction:
    wrongly refusing a story is worse than wrongly not refusing one,
    because a refusal is the thing that cannot be argued with.
    """
    tail = (output or "")[-4000:]
    # FIRST, and ahead of `timed_out` deliberately. A pod stuck pulling
    # an image also hits its deadline, and classifying that as a timeout
    # against a DECLARED budget makes it a `fail` — telling the executor
    # its suite is too slow when the registry was down.
    if infra_error:
        return unrunnable(infra_error, command=command, output=tail,
                          infra=True)
    if timed_out:
        # The DETAIL matters as much as the classification: a model
        # reads this, and "timeout" alone sent it to edit setup files
        # for a suite that was merely slow.
        #
        # AND THE CLASSIFICATION TURNS ON WHO CHOSE THE BUDGET.
        #
        #   declared   the contract said this finishes in N seconds and
        #              it did not. That IS a statement about the work --
        #              the suite is slower than the thing being checked
        #              promised -- so it is a FAIL, and the executor is
        #              told the truth: make it faster or declare more.
        #   default    nobody chose 300s; it is what happens when no one
        #              says. Exceeding it means NOTHING WAS CHECKED, and
        #              a check that did not finish did not say the work
        #              is wrong. ERROR, which spends a strike and does
        #              not condemn the story.
        #
        # Both directions were ERROR before, which made a declared
        # budget unenforceable: a contract could ask for two hours,
        # blow it, and spend the adjudicator's strikes on what reads as
        # an environment fault. A budget nothing enforces is a comment.
        from kernel.verifier.budget import DEFAULT_S, timeout_reason
        return Outcome(FAIL if timeout_declared else ERROR, {
            "reason": timeout_reason(timeout_s or DEFAULT_S,
                                     timeout_declared),
            "timeout_s": timeout_s or DEFAULT_S,
            "timeout_declared": bool(timeout_declared),
            "command": command, "output": tail})
    # NOTHING EXPLAINED THIS ENDING. A timeout legitimately has no exit
    # code — the container was killed, and the flag above carries the
    # meaning — so this sits after that branch and not before it. What
    # is left is a driver that returned no code and no reason, and
    # inventing a failing one to fill the hole is exactly how the
    # Kubernetes preemption bug happened.
    if exit_code is None:
        return unrunnable("the run reported no exit code, so it reported no "
                          "verdict", command=command, output=tail, infra=True)
    if exit_code == 0:
        return Outcome(PASS, {"command": command, "output": tail})
    if exit_code == 127:
        return Outcome(ERROR, {"reason": "command not found — the image "
                                         "does not have what this check "
                                         "needs, so nothing was checked",
                               "command": command, "exit_code": 127,
                               "output": tail})
    return Outcome(FAIL, {"reason": "non-zero exit", "command": command,
                          "exit_code": exit_code, "output": tail})


def unrunnable(reason: str, **detail) -> Outcome:
    """The criterion could not be run. Never a refusal.

    An unknown criterion kind lands here rather than failing: a contract
    naming a kind this Verifier does not implement is a deployment
    mismatch, and refusing the story would blame the work for it.
    """
    return Outcome(ERROR, {"reason": reason, **detail})
