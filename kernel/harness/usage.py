"""Usage normalisation — E5b increment of Stage E.

Ported from upstream openclaw/openclaw usage.ts (v2026.8.1).
This is a deliberate exception to vendor-don't-stub: ~55 lines of alias
resolution table, not 96 files of interlocking security code, and
TypeScript cannot be vendored into a Python worker.

NormalizedUsage field names match upstream exactly:
  input, output, cacheRead, cacheWrite, thinking
so ES2's cost shape (_sum_spend in predicates.py) is unchanged.

Thinking tokens are a SUBSET of output — always <= output.
Non-thinking output = output - thinking.
Undefined when the model does not use extended thinking.

The negative-prompt-tokens clamp handles providers (pi-ai OpenAI-format)
that pre-subtract cached_tokens from prompt_tokens upstream, producing
negative values when cached_tokens > prompt_tokens.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class NormalizedUsage:
    """Normalised token usage — field names match upstream exactly."""
    input: Optional[int] = None
    output: Optional[int] = None
    cacheRead: Optional[int] = None
    cacheWrite: Optional[int] = None
    total: Optional[int] = None
    thinking: Optional[int] = None


def _as_finite_int(value) -> Optional[int]:
    """Extract a finite number, or None."""
    if not isinstance(value, (int, float)):
        return None
    if not isinstance(value, int) and not value == value:  # NaN check
        return None
    if isinstance(value, float) and (value == float('inf') or value == float('-inf')):
        return None
    return int(value)


def normalize_usage(raw) -> Optional[NormalizedUsage]:
    """Normalise raw provider usage into NormalizedUsage.

    Handles six naming families for input/output, four provider-specific
    spellings of cache, thinking under both output_tokens_details.thinking_tokens
    and a top-level field, and the negative-prompt-tokens clamp.

    Ported from upstream usage.ts normalizeUsage() — test cases in
    test_e5b.py test 9 prove conformance.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None

    # ── Input: six naming families ───────────────────────────────────────
    # Some providers (pi-ai OpenAI-format) pre-subtract cached_tokens from
    # prompt_tokens upstream.  When cached_tokens > prompt_tokens the result
    # is negative, which is nonsensical.  Clamp to 0.
    raw_input = _as_finite_int(
        raw.get("input")
        if raw.get("input") is not None else raw.get("inputTokens")
        if raw.get("inputTokens") is not None else raw.get("input_tokens")
        if raw.get("input_tokens") is not None else raw.get("promptTokens")
        if raw.get("promptTokens") is not None else raw.get("prompt_tokens")
    )
    inp = max(raw_input, 0) if raw_input is not None and raw_input < 0 else raw_input

    # ── Output ───────────────────────────────────────────────────────────
    output = _as_finite_int(
        raw.get("output")
        if raw.get("output") is not None else raw.get("outputTokens")
        if raw.get("outputTokens") is not None else raw.get("output_tokens")
        if raw.get("output_tokens") is not None else raw.get("completionTokens")
        if raw.get("completionTokens") is not None else raw.get("completion_tokens")
    )

    # ── Cache read: four spellings ───────────────────────────────────────
    # Moonshot/Kimi uses cached_tokens for cache read count.
    # Kimi K2 uses prompt_tokens_details.cached_tokens for automatic prefix caching.
    ptd = raw.get("prompt_tokens_details") or {}
    cache_read = _as_finite_int(
        raw.get("cacheRead")
        if raw.get("cacheRead") is not None else raw.get("cache_read")
        if raw.get("cache_read") is not None else raw.get("cache_read_input_tokens")
        if raw.get("cache_read_input_tokens") is not None else raw.get("cached_tokens")
        if raw.get("cached_tokens") is not None else ptd.get("cached_tokens")
    )

    # ── Cache write ──────────────────────────────────────────────────────
    cache_write = _as_finite_int(
        raw.get("cacheWrite")
        if raw.get("cacheWrite") is not None else raw.get("cache_write")
        if raw.get("cache_write") is not None else raw.get("cache_creation_input_tokens")
    )

    # ── Total ────────────────────────────────────────────────────────────
    total = _as_finite_int(
        raw.get("total")
        if raw.get("total") is not None else raw.get("totalTokens")
        if raw.get("totalTokens") is not None else raw.get("total_tokens")
    )

    # ── Thinking tokens ──────────────────────────────────────────────────
    # Anthropic nests them under output_tokens_details.thinking_tokens;
    # future providers may surface a top-level thinking_tokens field.
    otd = raw.get("output_tokens_details") or {}
    thinking = _as_finite_int(
        otd.get("thinking_tokens")
        if otd.get("thinking_tokens") is not None else raw.get("thinking_tokens")
    )

    # ── All None → undefined ─────────────────────────────────────────────
    if all(v is None for v in (inp, output, cache_read, cache_write, total)):
        return None

    return NormalizedUsage(
        input=inp,
        output=output,
        cacheRead=cache_read,
        cacheWrite=cache_write,
        total=total,
        thinking=thinking,
    )


def has_nonzero_usage(usage) -> bool:
    """True if usage has any finite positive field."""
    if usage is None:
        return False
    if isinstance(usage, dict):
        return any(
            isinstance(v, (int, float)) and v > 0
            for v in [usage.get("input"), usage.get("output"),
                       usage.get("cacheRead"), usage.get("cacheWrite"),
                       usage.get("total")]
        )
    if isinstance(usage, NormalizedUsage):
        return any(
            v is not None and v > 0
            for v in [usage.input, usage.output, usage.cacheRead,
                       usage.cacheWrite, usage.total]
        )
    return False
