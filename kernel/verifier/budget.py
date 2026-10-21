"""How long a criterion may take, and who gets to say.

THE DEFECT. Every timeout in the verifier is a hardcoded 300 seconds and
NO caller passes one -- `verify_claim`, `run_command`, `evaluate` all
default and nothing overrides. A real repository's test suite runs
longer than five minutes, so it times out; a timeout is `error` (rightly
-- a check that did not finish did not say the work is wrong); three
errors spend the adjudicator's strikes; and the executor is handed a
message telling it to fix its manifest, `.agentos/setup.sh` and env
setup. It will edit setup files it has no problem with, re-propose, time
out again, and fail with "N legs without converging" and no recorded
reason.

The bug was not the ERROR classification for a DEFAULT budget -- that
reasoning is sound and stands. It was that there was no way to SAY a
suite needs longer.

AND ONCE THERE IS, THE CLASSIFICATION SPLITS. A default budget exceeded
is still `error`: nobody chose 300s, so the overrun says nothing about
the work. A DECLARED budget exceeded is a `fail`, because somebody wrote
the number down and the work did not meet it -- see
kernel/verifier/outcome.py. Leaving both as `error` made the declaration
unenforceable, which is the same as not having it: a contract could ask
for two hours, blow it, and spend the adjudicator's strikes on what
reads to everyone as an environment fault.

WHERE IT CAN BE SAID, most specific first:

    criterion["spec"]["timeout_s"]     this one check
    contract["verify_timeout_s"]       every check in this contract
    DEFAULT_S                          300, unchanged

CAPPED, because a criterion holds an admission slot and a verifier pod
for its whole run. An unbounded declaration is a way to hold both for
ever by writing a number in a file, and the contract is authored by the
same model whose work is being checked -- so this is one of the places
where "the contract says so" is not sufficient authority.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# TWENTY MINUTES, raised from five on 21 September 2026.
#
# WHAT 300 COST. Run 255's suite ran 13.4 minutes -- a perfectly ordinary
# integration suite -- and a check that had not declared a budget would
# have been killed at five, reporting a TIMEOUT that reads to everyone
# downstream as the work being slow rather than the default being small.
# The schema tells a model "a full test suite is a perfectly good check;
# never trim one to be quick", and then the unstated default contradicted
# it for any suite over five minutes.
#
# WHY A DEFAULT AT ALL, AND WHY NOT MORE. A declared `timeout_s` is still
# the right answer and still beats this: the model proposes it, a person
# approves it on the card, and it goes up to MAX_S. This number only
# covers the check whose author did not think about time -- so it wants
# to be generous enough that an ordinary suite is not cut off, and small
# enough that a WEDGED check is noticed. A criterion holds an admission
# slot and a verifier pod for its whole run, and twenty minutes of a
# hung one is a cost somebody pays.
#
# IT IS ALSO NOW WELL CLEAR OF `BASH_MAX_S`, which matters for
# `run_check`: an undeclared check on a 300s default was only 2.5x the
# foreground ceiling, so the tool that exists to run a LONG check barely
# ran a longer one.
DEFAULT_S = 1200.0

# Two hours. Long enough for a monorepo's full suite, short enough that
# a wedged criterion is noticed the same working day. A contract asking
# for more is clamped and told, rather than refused -- refusing would
# turn a too-eager number into a story that cannot verify at all.
MAX_S = 7200.0

# Below this a criterion cannot even start a container, so a small
# number is far more likely to be a mistake (milliseconds mistaken for
# seconds) than an intention.
MIN_S = 5.0


def timeout_for(contract: dict | None, criterion: dict | None) -> float:
    """Seconds this criterion may run for."""
    declared = None
    spec = (criterion or {}).get("spec") or {}
    for source in (spec.get("timeout_s"), (contract or {}).get("verify_timeout_s")):
        if source is None:
            continue
        try:
            declared = float(source)
        except (TypeError, ValueError):
            logger.warning("verify: timeout_s %r is not a number; using the "
                           "default of %.0fs", source, DEFAULT_S)
            continue
        break

    if declared is None:
        return DEFAULT_S

    if declared > MAX_S:
        logger.warning("verify: timeout_s %.0fs exceeds the %.0fs ceiling; "
                       "clamped. A criterion holds an admission slot and a "
                       "verifier pod for its whole run.", declared, MAX_S)
        return MAX_S
    if declared < MIN_S:
        logger.warning("verify: timeout_s %.0fs is below the %.0fs floor; "
                       "using the floor. A criterion cannot start a "
                       "container in less.", declared, MIN_S)
        return MIN_S
    return declared


def timeout_reason(seconds: float, declared: bool) -> str:
    """What to put in the `error` detail, which a model will read.

    NAMES THE FIELD when the budget was the default, because the run
    that hits this is otherwise told to fix an environment that is not
    broken -- it edits setup files, re-proposes, times out again, and
    burns its strikes on a defect it does not have.
    """
    if declared:
        return (f"the check ran for its declared {seconds:.0f}s budget and "
                f"did not finish. Nothing was checked. Either the work is "
                f"slower than the contract expected, or the check does not "
                f"terminate.")
    return (f"the check ran for the default {seconds:.0f}s and did not "
            f"finish. Nothing was checked, and this is NOT an environment "
            f"fault -- do not edit the manifest or setup on account of it. "
            f"If the suite legitimately needs longer, the contract declares "
            f"it: `verify_timeout_s` for every check, or `timeout_s` in a "
            f"criterion's spec for one.")


def was_declared(contract: dict | None, criterion: dict | None) -> bool:
    spec = (criterion or {}).get("spec") or {}
    return (spec.get("timeout_s") is not None
            or (contract or {}).get("verify_timeout_s") is not None)
