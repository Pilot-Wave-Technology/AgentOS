"""Leg predicates — E3 increment of Stage E.

Three predicates end a leg: token budget, step budget, lease clock.
The harness evaluates them after every step; the model cannot end a leg.

Token accounting matches NormalizedUsage from agent_os_openclaw/src/agents/usage.ts:
  cost = { input, output, cacheRead, cacheWrite, thinking? }
  spend = input + output + cacheRead + cacheWrite
Thinking is NOT added — it is a subset of output. See usage.ts line 40-43.

budget_basis is per-leg, derived from the adapter's three-valued reasoning
capability (see AdapterCapabilities.reasoning in models/types.py):
  measured  — token count is complete.  Applies when reasoning is "none"
              (no thinking), "reported" (thinking is a separate number), or
              "unreported" where thinking is included in output (Gemini).
  estimated — token count is incomplete; the predicate applies
              reasoning_multiplier to estimate total spend.  Would apply to a
              provider that does thinking but excludes it from output.
              **No live consumer today** — both current adapters (Anthropic
              "reported", Gemini "unreported" with thinking in output) produce
              complete counts.  The multiplier is retained for the future case
              but has never fired in a real run.
"""

from dataclasses import dataclass
import time
from typing import Optional


# ── Provisional defaults — labelled as such ────────────────────────────────
# LEG_TOKEN_BUDGET: placeholder. E5b resolves ceiling from adapter capabilities,
# E5c resolves floor. Do NOT pin as a global constant.
DEFAULT_LEG_TOKEN_BUDGET = 100_000  # provisional
DEFAULT_LEG_STEP_BUDGET = 200       # provisional
DEFAULT_LEG_LEASE_CLOCK_SECONDS = 600.0  # provisional

# Reasoning multiplier: conservative (rounds up).  When basis=estimated, the
# predicate inflates raw spend to cover unreported thinking tokens.
# NO LIVE CONSUMER (ES3): both current adapters produce complete counts
# (Anthropic: "reported", Gemini: "unreported" but thinking included in output).
# Retained for the future case where a provider does thinking but excludes it
# from the output token count.  The value (6.25) was derived from a single
# 108-token trivial prompt and has never been validated at scale (pending E5c).
DEFAULT_REASONING_MULTIPLIER = 6.25  # no live consumer — see ES3 cycle


@dataclass(frozen=True)
class PredicateResult:
    """Result of evaluating a leg predicate."""
    fired: bool
    predicate: str
    detail: dict


def _cache_weights(model: Optional[str]) -> tuple:
    """(read, write) price multipliers for a model, or parity if unknown.

    Unknown resolves to 1.0/1.0 — the conservative direction, since it
    overstates rather than hides cost, and a model we cannot identify is
    exactly when a quiet understatement would be worst.
    """
    if not model:
        return (1.0, 1.0)
    # CONFIGURED RATES WIN. They are the actual published numbers rather
    # than a ratio remembered in the registry, and two sources that can
    # disagree is one source too many. The registry ratios stay as the
    # fallback so the budget backstop still works with no rates set.
    try:
        from kernel.harness.models.pricing import cache_ratios
        ratios = cache_ratios(model)
        if ratios is not None:
            return ratios
    except Exception:                                       # noqa: BLE001
        pass
    try:
        from kernel.harness.models.registry import resolve
        spec = resolve(model)
    except Exception:                                       # noqa: BLE001
        return (1.0, 1.0)
    return (spec.cache_read_weight, spec.cache_write_weight)


def _sum_spend(cost: Optional[dict]) -> float:
    """Billable fields from a cost dict, at the provider's own weights.

    spend = input + output + cacheRead x w_read + cacheWrite x w_write
    Thinking is NOT added — it is a subset of output (usage.ts line 40-43).

    CACHED TOKENS ARE NOT BILLED AT PARITY, and adding all four fields
    flat overstated the bill by roughly the cached share — measured at
    48% coverage on Anthropic, which bills a read at a tenth. The weight
    comes from the model in the cost dict, which is why steps journal
    it.

    Two questions, two sums, and the weights are exactly where they
    diverge: `peak_context` counts a cached token at FULL weight,
    because it occupies the window like any other, while here it counts
    at what it costs. One number could never have served both.

    Token-equivalents at the input price, not currency: output is left
    at 1.0 though it bills several times higher. Good for comparing
    runs, not a bill.
    """
    if not cost:
        return 0.0
    w_read, w_write = _cache_weights(cost.get("model"))
    return (
        (cost.get("input") or 0)
        + (cost.get("output") or 0)
        + (cost.get("cacheRead") or 0) * w_read
        + (cost.get("cacheWrite") or 0) * w_write
    )


def peak_context(cost: Optional[dict]) -> int:
    """What the model had to hold at once on this step.

    `input + cacheRead + cacheWrite`. NOT output — the reply is
    generated, not held. A cached token still occupies the window, which
    is why reads are in here and why the COST view weights them
    differently.

    CACHEWRITE IS IN HERE BECAUSE THE PROVIDERS DISAGREE ABOUT WHERE IT
    LIVES, and getting this wrong undercounts a real window. Anthropic's
    `input_tokens` is defined as the tokens NOT read from and NOT
    written to a cache, so on the turn that CREATES a cache the cached
    prefix appears only under `cache_creation_input_tokens` — present in
    the request, absent from both other fields. Gemini never reports a
    per-step write at all, because its cache is created by a separate
    `caches.create` call, so this term is always zero there. One
    expression, correct on both, rather than a per-provider branch that
    only one provider's runs would ever exercise.
    """
    if not cost:
        return 0
    return int((cost.get("input") or 0) + (cost.get("cacheRead") or 0)
               + (cost.get("cacheWrite") or 0))


def check_context_budget(steps: list[dict], budget: int) -> PredicateResult:
    """Has the conversation grown past what a leg should hold?

    THIS IS WHAT ENDS A LEG, and it replaces a cumulative spend sum that
    was doing the job badly. Measured on a real leg: 22 steps, summed
    spend 190,719, peak context 17,397. The leg ended on a 200,000
    budget having never touched 1.7% of the window it was sized from.
    Summing counts step 1's tokens again on every later step, because
    each request carries the whole conversation.

    The PEAK rather than the last step: a leg whose context shrank after
    a large read has still been as big as it was, and the window it
    needed is the largest it reached.
    """
    peak = max((peak_context(s.get("cost")) for s in steps), default=0)
    fired = budget != -1 and peak > budget
    return PredicateResult(
        fired=fired,
        predicate="context_budget_exhausted",
        detail={"peak_context": peak, "budget": budget,
                "steps": len(steps)},
    )


def check_token_budget(
    steps: list[dict],
    budget: int,
    basis: str,
    reasoning_multiplier: float = DEFAULT_REASONING_MULTIPLIER,
) -> PredicateResult:
    """Check whether cumulative token spend exceeds the leg budget.

    Args:
        steps: list of step_journal rows (each has a 'cost' key, may be None).
        budget: the leg token budget (provisional — not a global constant).
        basis: 'measured' or 'estimated' — from adapter capability declaration.
        reasoning_multiplier: applied when basis='estimated' to cover the
            thinking-token gap. Conservative means > 1.0 (rounds up).
    """
    raw_spend = sum(_sum_spend(s.get("cost")) for s in steps)

    if basis == "estimated":
        # No live consumer today — retained for a provider that declares
        # reasoning="unreported" with thinking_in_output=False.
        # Reachable via red-check (flip Gemini's thinking_in_output).
        effective_spend = raw_spend * reasoning_multiplier
    elif basis == "measured":
        effective_spend = raw_spend
    else:
        raise ValueError(f"budget_basis must be 'measured' or 'estimated', got {basis!r}")

    # -1 is unbounded. Checked here rather than by the caller, because
    # the caller that forgets is the one that ends every leg at step 0.
    fired = budget != -1 and effective_spend > budget
    return PredicateResult(
        fired=fired,
        predicate="token_budget_exhausted",
        detail={
            "raw_spend": raw_spend,
            "effective_spend": effective_spend,
            "budget": budget,
            "basis": basis,
            "reasoning_multiplier": reasoning_multiplier if basis == "estimated" else None,
        },
    )


def check_step_budget(steps: list[dict], budget: int) -> PredicateResult:
    """Check whether step count has reached the leg step budget.

    -1 IS UNBOUNDED AND MUST BE CHECKED FIRST. `count >= -1` is true for
    every non-negative count, so without this the predicate fires on
    step zero and an "unbounded" leg is the shortest one possible — a
    setting that does the exact opposite of what it says.
    """
    # A STEP THE PROVIDER DID NOT ANSWER IS NOT A STEP THE RUN TOOK.
    #
    # This counted len(steps), so an empty response — no content, no tool
    # call, zero output tokens — consumed the run's ceiling. Gemini does
    # that on a MEASURED 33% of calls for certain (prompt, tool-surface)
    # pairs, so a third of every leg's budget was being spent on the
    # provider saying nothing, and the leg then ended reporting that the
    # RUN had run out of steps.
    #
    # Fifth instance of the same defect: a provider fact recorded as a
    # verdict on the work, after provider_empty-as-no_tool, exit 127 as
    # fail, an outage as FAILED, and cumulative spend as a floor.
    #
    # The run cannot be unbounded on empties either — see
    # check_empty_ceiling, which ends it on its own predicate.
    count = sum(1 for step in steps if not step.get("empty"))
    fired = budget != -1 and count >= budget
    return PredicateResult(
        fired=fired,
        predicate="step_budget_exhausted",
        detail={"step_count": count, "budget": budget,
                "empty_steps": len(steps) - count},
    )


def check_empty_ceiling(steps: list[dict], ceiling: int) -> PredicateResult:
    """Has the provider answered nothing, too many times running?

    Empty steps no longer consume the step budget, which without this
    would make a provider that returns nothing an INFINITE leg. They end
    the leg on their own predicate instead, so the report says the
    provider stopped answering rather than that the run ran out of room.

    CONSECUTIVE, not cumulative: the measured behaviour is deterministic
    per prompt-and-tool-surface, so a run that hits some empties and then
    makes progress is healthy, and a cumulative counter would end it
    anyway on a long leg.
    """
    run = 0
    for step in steps:
        run = run + 1 if step.get("empty") else 0
    fired = ceiling != -1 and run >= ceiling
    return PredicateResult(
        fired=fired,
        predicate="provider_not_answering",
        detail={"consecutive_empty": run, "ceiling": ceiling},
    )


def check_lease_clock(start_time: float, limit: float) -> PredicateResult:
    """Check whether wall time since start exceeds the lease clock limit.

    Args:
        start_time: monotonic timestamp (time.monotonic()) of leg start.
        limit: seconds allowed for this leg.
    """
    elapsed = time.monotonic() - start_time
    fired = elapsed > limit
    return PredicateResult(
        fired=fired,
        predicate="lease_clock_exhausted",
        detail={"elapsed": elapsed, "limit": limit},
    )
