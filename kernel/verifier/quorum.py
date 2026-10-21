"""F3 — several runs of one criterion, and what they mean together.

F1 built `run_index` and a `flake` value; F2 and F2b shipped a refusal
path that used neither. `refuses()` was `any(passes == "fail")` over a
SINGLE run, so:

    one flaky test  ->  fail  ->  the claim is refused
                    ->  the proposal is REJECTED, arming E5's fence
                    ->  the model must invent a DIFFERENT reason set
                        before it can claim the same finished work again

A story that was genuinely done, killed by a test that would have passed
on the next run and then blocked from saying so. That is precisely the
failure R1 was written to prevent, and F2b armed the fence without the
quorum that makes it safe.

DISAGREEMENT IS A FLAKE, NOT A MAJORITY VOTE. Two of three failing could
be read as `fail`, and this deliberately does not: the cost of being
wrong is wildly asymmetric. A wrong refusal is close to unrecoverable —
it blocks re-claiming and the model has no honest way to change its
reason set. A wrong pass is caught by the Reviewer and by the human who
still approves the merge, neither of which the Verifier replaces. So
unanimity to fail, unanimity to pass, and anything else is a flake that
is RECORDED and does not refuse.

WHAT THIS CANNOT SEE. A criterion that flakes in the PASSING direction —
green when it should be red — is invisible here, because a pass is not
re-run. Detecting it means running every criterion N times on the happy
path, which multiplies the cost of every verification to catch a case
the Reviewer and the human gate already stand behind. Named rather than
quietly accepted.
"""

from __future__ import annotations

import logging

from kernel.verifier.outcome import ERROR, FAIL, FLAKE, PASS, Outcome

logger = logging.getLogger(__name__)

# How many times a criterion may run before the quorum settles it. Only
# a criterion that does NOT pass first time costs more than one run, so
# the happy path pays nothing for this.
DEFAULT_RUNS = 3


def runs_allowed(contract: dict) -> int:
    """The quorum size this contract asks for. Bounded, and never below 2.

    A quorum of one is not a quorum — it is the single-run behaviour this
    module exists to replace, and letting a contract declare it would let
    a contract turn the defence off.
    """
    spec = (contract.get("workspace_spec") or {}).get("quorum") or {}
    try:
        runs = int(spec.get("runs", DEFAULT_RUNS))
    except (TypeError, ValueError):
        runs = DEFAULT_RUNS
    return max(2, min(runs, 7))


# How much of each run's output survives into the audit trail. The
# settled outcome carries the failing run in full; these are the
# per-run rows behind it, and a criterion re-run three times should not
# write three copies of a megabyte of pytest output.
RUN_TAIL_CHARS = 800


def observations(outcomes: list[Outcome]) -> list[dict]:
    """Each run, bounded, for `criterion_results`.

    F1 built `run_index` and a unique index across it so the table could
    hold the EVIDENCE while `verdicts` holds the decision. The quorum
    votes in memory; without this it would also record in memory, and
    the reason a verdict said `flake` would be underivable an hour
    later.
    """
    return [{"passes": o.passes,
             "exit_code": (o.detail or {}).get("exit_code"),
             "reason": (o.detail or {}).get("reason"),
             "output": ((o.detail or {}).get("output") or "")[-RUN_TAIL_CHARS:]}
            for o in outcomes]


def settle(outcomes: list[Outcome]) -> Outcome:
    """One value from several runs of the same criterion.

    Errors are set aside before the vote, not counted in it. A run that
    could not happen is not evidence either way, and letting one outvote
    two real results would decide a story on a container that failed to
    start.
    """
    if not outcomes:
        return Outcome(ERROR, {"reason": "no runs", "_runs": []})

    ran = [o for o in outcomes if o.passes in (PASS, FAIL)]
    if not ran:
        # Every attempt failed to run. Keep the first one's detail: it
        # says WHY, and "the check could not run" with no reason is a
        # refusal-shaped hole in the record.
        first = outcomes[0]
        return Outcome(ERROR, {**(first.detail or {}),
                               "runs": len(outcomes),
                               "_runs": observations(outcomes),
                               "reason": first.detail.get("reason")
                               or "no run completed"})

    values = {o.passes for o in ran}
    if values == {PASS}:
        return Outcome(PASS, {**(ran[-1].detail or {}), "runs": len(ran),
                              "_runs": observations(outcomes)})
    if values == {FAIL}:
        # The verdict records WHY it failed, not merely that it did —
        # so the LAST failing run's detail travels, output and all.
        return Outcome(FAIL, {**(ran[-1].detail or {}), "runs": len(ran),
                              "_runs": observations(outcomes)})

    # Disagreement. Carry a failing run's detail: a person chasing a
    # flake needs the output from the run that failed, not the one that
    # passed and said nothing.
    failing = next(o for o in ran if o.passes == FAIL)
    tally = {v: sum(1 for o in ran if o.passes == v) for v in values}
    logger.info("criterion flaked across %d runs: %s", len(ran), tally)
    return Outcome(FLAKE, {
        **(failing.detail or {}),
        "runs": len(ran),
        "tally": tally,
        "_runs": observations(outcomes),
        "reason": ("the criterion disagreed with itself across runs; "
                   "recorded and NOT refused, because a check that "
                   "cannot make up its mind has not shown the work wrong"),
    })


def needs_another_run(outcomes: list[Outcome], allowed: int) -> bool:
    """Should this criterion run again?

    Only a criterion that has not passed unanimously so far. The first
    PASS settles it, which is what keeps the happy path at exactly one
    container per criterion — a quorum that costs three runs of a green
    suite is a quorum somebody turns off.
    """
    if len(outcomes) >= allowed:
        return False
    if not outcomes:
        return True
    return any(o.passes != PASS for o in outcomes)
