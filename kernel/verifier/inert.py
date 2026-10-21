"""G1b — did anything in the tree affect this check's answer?

G0 fixed half a defect and named the other half. Exit 127 is the shell's
reserved "command not found", so a criterion whose runner is missing can
be reported as `error` rather than as the work being wrong. But the form
that actually bit — the first contract ever written by hand in this
console — was

    python3 -m pytest -q test_counter.py

and with pytest absent that exits **1** with `No module named pytest`.
Indistinguishable, by exit code, from a test that ran and failed. F11's
labels cannot see it either: a missing-runner `goal` fails at approval
(which looks correct) and fails at verification (which looks like
incomplete work), so the story is refused for ever and the refusal names
the code.

THE INSTRUMENT. Run the failing criteria a second time against git's
canonical **empty tree** — `4b825dc…`, an object present in every
repository, so this is the same code path a real verification takes and
not a synthetic fixture. Then compare.

    different answer     something in the tree moved the result. The
                         check is measuring the work.
    identical answer     nothing in the tree moved the result. The check
                         is measuring the IMAGE, or checking for the
                         absence of something.

IT WARNS, IT DOES NOT BLOCK, and that is a deliberate limit rather than
timidity. `test -f WORK.md` before WORK.md exists answers identically on
both trees and is a perfectly good goal; `python3 -m pytest` with no
pytest answers identically and can never pass. **Nothing can separate
them** — the real question is "could any tree make this pass", which is
undecidable. So the honest thing is to say what was measured and let the
person decide, at the moment they are already reading the plan.

The warning is useful in BOTH cases, which is what makes it worth
showing rather than noise: for the existence check it says *nothing
currently produces this*, and for the broken runner it says *this will
never pass*.

ONLY FAILURES ARE PROBED. A criterion that passed obviously ran, and one
that errored already reports itself. That halves the container cost and
is not an optimisation so much as the correct scope: the instrument
exists to explain failures.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# git's empty tree. Every repository has it without anyone creating it —
# it is a property of the object model, not a fixture that could drift.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def same_answer(a, b) -> bool:
    """Did these two runs say the same thing, for the same reason?

    Outcome AND exit code AND output. Outcome alone is far too coarse:
    `pytest -q` on a real tree exits 1 for failures and on an empty tree
    exits 5 for "no tests collected", and both are `fail` — treating
    those as the same answer would flag every working pytest check in
    the system.
    """
    if a.passes != b.passes:
        return False
    if a.detail.get("exit_code") != b.detail.get("exit_code"):
        return False
    return (a.detail.get("output") or "") == (b.detail.get("output") or "")


async def probe(driver, contract: dict, criteria: list[dict],
                outcomes: list["Outcome"], repo: str,
                timeout: float = 120.0, workspace_host: str = "",
                execution_id: int | None = None) -> dict:
    """Which of these criteria answer identically against nothing?

    Returns {index: note}. An empty dict is the ordinary case and costs
    nothing when no criterion failed.

    `outcomes` IS A LIST OF BARE Outcomes, index-aligned with `criteria`
    — NOT the (criterion, Outcome) pairs `verify_claim` returns. The
    annotation is there because the two callers disagreed about it and
    neither was obviously wrong: this function takes `criteria`
    separately and indexes it, so it needs the outcomes alone, and
    dryrun.py strips the pairing before calling. A test called it with
    pairs. Both crashed, in opposite directions, on the failure path.

    Callers holding pairs want `[o for _c, o in pairs]`.
    """
    from kernel.verifier.verify import verify_claim

    # BARE OUTCOMES, NOT PAIRS. `verify_claim` returns
    # list[tuple[dict, Outcome]], and dryrun.py strips the pairing before
    # calling here -- `[o for c, o in zip(criteria, outcomes) if
    # c["kind"] == "command"]` -- because this function already takes
    # `criteria` separately and indexes it. Unpacking a pair here raised
    # "cannot unpack non-iterable Outcome object", which means G1b's
    # inert probe has never run: it fires only when a criterion FAILS,
    # and every dry-run that reached it crashed instead.
    #
    # The callee is the wrong side of this. The caller's two lists are
    # filtered to command criteria and index-aligned, which is what
    # `criteria[i]` below depends on.
    failing = [i for i, o in enumerate(outcomes) if o.passes == "fail"]
    if not failing:
        return {}

    try:
        # PASSED ON even though this archives the EMPTY TREE: the branch
        # that decides where to archive FROM is `archive_is_remote(driver)`,
        # not the commit, so a pod substrate still crosses — and without
        # an address it refused, turning this extra opinion into a
        # warning on every failing check.
        against_nothing = await verify_claim(
            driver, contract, [criteria[i] for i in failing], repo,
            EMPTY_TREE, timeout=timeout, workspace_host=workspace_host,
            execution_id=execution_id)
    except Exception as e:                                  # noqa: BLE001
        # Never fatal. This is an extra opinion about a check, and
        # failing to obtain it must not stop a plan being approved —
        # the dry-run's own answer is unaffected.
        logger.warning("inert probe could not run: %s", e)
        return {}

    inert = {}
    for slot, i in enumerate(failing):
        if slot >= len(against_nothing):
            break
        # ASYMMETRIC ON PURPOSE, and this is where it bit. `outcomes` is
        # a list of bare Outcomes -- the caller strips the pairing -- but
        # `against_nothing` comes straight from `verify_claim`, which
        # returns (criterion, outcome). Treating both the same way is
        # what produced "'Outcome' object is not subscriptable" once the
        # first unpacking bug above was fixed.
        if same_answer(outcomes[i], against_nothing[slot][1]):
            inert[i] = (
                "this failed IDENTICALLY against an empty tree, so nothing "
                "in the code affected the result. Either it is checking "
                "for something that does not exist yet — fine, and it will "
                "pass once you create it — or the image cannot run it at "
                "all, in which case it can never pass and will refuse the "
                "story for ever.")
    if inert:
        logger.info("inert probe: %d of %d failing criteria answered "
                    "identically against nothing", len(inert), len(failing))
    return inert
