"""Everything that can end a leg mid-loop, in the order that decides WHY.

THE ORDER IS THE CONTENT OF THIS FILE. Several of these are routinely
true at once at the end of a long leg, and only the first one asked gets
recorded as the cause. Getting that wrong does not lose a run -- it
misattributes one, which is worse, because the run then reads as a
different problem than it had. Six times in Stage H a true fact was
recorded as the wrong kind of fact.

  1. CONTEXT, because it is what a leg boundary is FOR: stop, write a
     brief, resume before the window fills.
  2. SPEND, the cost backstop, so a leg that is cheap but enormous still
     ends and one that is small but expensive still ends.
  3. THE STALL CLOCK, ahead of everything that counts WORK. Its own
     comment says "FIRST" and means first of those: a run that has
     repeated itself eight times has not exhausted a step budget,
     and ending it on one would report the wrong cause. A real-model baseline ran with
     this disabled "so it would not interfere" and looped FORTY-FOUR
     times saying "done".
  4. THE EMPTY CEILING, before the step budget, because a leg that is
     only empties has not exhausted anything the RUN did.
  5. THE STEP BUDGET, then 6. THE LEASE CLOCK.
  7. The provider's own stop reasons, last, because they describe the
     turn and not the leg.

RESHUFFLING THESE IS A BEHAVIOUR CHANGE even though no run's outcome
moves. Do not reorder them to group the cheap checks together.

THERE IS NO WAY TO SWITCH THE STALL CLOCK OFF. See point 3.
"""

from __future__ import annotations

import logging

from kernel.harness.predicates import (
    check_context_budget,
    check_empty_ceiling,
    check_lease_clock,
    check_step_budget,
    check_token_budget,
)
from kernel.harness.stall import check_stall

logger = logging.getLogger(__name__)


class Fired:
    """A predicate result the harness synthesised rather than measured.

    The real checks in `predicates.py` return their own objects; parking,
    cancellation and a provider stop reason have no check behind them and
    still have to travel as the same shape. This replaces four copies of
    `type("PR", (), {...})()`, which was the same class written out four
    times and one place for the field names to drift.

    `fired` is True for consistency with a measured result. Nothing reads
    it on a synthesised one today, and that is exactly why it should be
    right: the next reader will not know which kind it holds.
    """

    fired = True

    def __init__(self, predicate: str, detail: dict) -> None:
        self.predicate = predicate
        self.detail = detail


# THE MODEL STOPPED TALKING, AND THAT IS NOT A PREDICATE. It ends the
# leg with `predicate: None` and `stopped: False` -- the return value
# has always distinguished "a rule ended this" from "the work ran out",
# and the console, the card sweeper and MAX_LEGS all read the
# difference. A `Fired(None, ...)` here would make every completed leg
# report as stopped.
MODEL_DONE = object()


def leg_ending(step_records: list[dict], response, *, leg, budget_basis: str,
               step_budget: int, start_time: float,
               lease_clock_seconds: float, adapter,
               execution_id, generation, tool_executor=None):
    """The first thing that ends this leg, or None to keep going.

    Returns a predicate result (measured or `Fired`), the `MODEL_DONE`
    sentinel, or None. Pure apart from the stall warning: it decides
    nothing and writes nothing, which is what makes the order above
    reviewable on its own.
    """
    # ── Evaluate predicates AFTER each step ──────────────────────────
    # CONTEXT FIRST, because it is what a leg boundary is FOR: stop,
    # write a brief, resume before the window fills. Spend is a cost
    # backstop and is checked after, so a leg that is cheap but
    # enormous still ends, and one that is small but expensive still
    # ends.
    context_result = check_context_budget(step_records,
                                          leg.context_tokens)
    if context_result.fired:
        return context_result

    token_result = check_token_budget(step_records, leg.spend_tokens,
                                      budget_basis)
    if token_result.fired:
        return token_result

    # H7's stall clock FIRST. A run that has repeated itself eight
    # times has not exhausted a budget, and ending it on one would
    # report the wrong cause — the sixth time in this stage that a
    # true fact would have been recorded as the wrong kind of fact.
    #
    # There is no way to switch this off. The measured failure is a
    # real-model baseline that ran with it disabled "so it would not
    # interfere" and then looped FORTY-FOUR times saying "done".
    stall_result = check_stall(step_records)
    if stall_result.fired:
        logger.warning(
            "execution %s gen %s: STALLED — %d consecutive identical "
            "steps (signature %s). Ending the leg; the run is busy and "
            "getting nowhere.",
            execution_id, generation, stall_result.detail["repeats"],
            stall_result.detail["signature"])
        return stall_result

    # Before the step budget, because a leg that is only empties has
    # not exhausted anything the run did.
    empty_result = check_empty_ceiling(
        step_records, getattr(adapter, "retry", None).empty_ceiling
        if getattr(adapter, "retry", None) else 6)
    if empty_result.fired:
        return empty_result

    step_result = check_step_budget(step_records, step_budget)
    if step_result.fired:
        return step_result

    # BLOCKED TIME IS CREDITED BACK. A leg that spent forty minutes
    # inside one `bash_wait` is not a leg that has been running for
    # forty minutes in the sense this clock means: the job had its own
    # budget (`job_max_s`), its own watchdog and a mirror row updating
    # throughout, and the leg did nothing but hold the result. Charging
    # it would mean a two-hour job -- which the platform offers -- always
    # ends the leg that waited for it before the model can act on the
    # answer. See `BridgeToolExecutor._wait_for_job`.
    #
    # It is credited and not ignored: everything the clock guards
    # against -- a model looping, a provider hanging, a leg that will
    # not converge -- is still charged in full.
    blocked = float(getattr(tool_executor, "_blocked_s", 0.0) or 0.0)
    lease_result = check_lease_clock(start_time + blocked,
                                     lease_clock_seconds)
    if lease_result.fired:
        return lease_result

    # ── Stop reasons that end the leg ────────────────────────────────
    if response.stop_reason in ("context_overflow", "error"):
        return Fired(response.stop_reason,
                     {"stop_reason": response.stop_reason})

    if response.stop_reason == "end_turn" and not response.tool_call:
        # Model is done talking but has no tool call — leg ends
        return MODEL_DONE

    return None
