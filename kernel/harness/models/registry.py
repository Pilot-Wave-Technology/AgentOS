"""Which models an agent may run on, and what each one can do.

CAPABILITIES ARE PER MODEL, NOT PER ADAPTER, and treating them as
per-adapter is a live defect. `GeminiAdapter.capabilities` is a property
of the class, so `gemini-3.1-flash-lite` and `gemini-3.1-pro-preview`
report the same `max_context` today — and **LR7 derives the leg token
budget from capabilities**, so a wrong context window is a wrong budget
on every leg of every run.

That is the reason this file exists. Choosing a model per agent is the
requirement; getting its capabilities right is what makes the choice
mean anything.

NOTHING HERE IS GUESSED. The Gemini figures come from the provider's own
`models.list()`, which reports `input_token_limit` and
`output_token_limit`. Where a number was not obtainable it is `None` and
the model is refused rather than run on an invented budget — the same
rule LR1 applies to reasoning: declare what you do not know, never
silently mix it with what you measured.

GEMINI 3 OR LATER, as a policy rather than an accident. `2.5-flash` at
temperature 0 returns `MALFORMED_FUNCTION_CALL` for three of five corpus
tasks, and it was the default for the whole of Stage G. Older models are
refused by name with that reason attached, so nobody rediscovers it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Modalities that are not an agent. A `-tts` or `-image` model will
# happily accept a request and never call a tool.
NOT_AN_AGENT = ("image", "tts", "transcribe", "embedding", "live",
                "translate")

# The floor, and why. Refusing by name beats discovering it again.
MIN_GEMINI_SERIES = 3
_GEMINI_SERIES = re.compile(r"^gemini-(\d+)")


@dataclass(frozen=True)
class ModelSpec:
    """One model an agent may be pointed at."""

    id: str
    provider: str
    max_context: int
    max_output: int
    # LR1's three-valued capability, per model rather than per vendor.
    reasoning: str = "unreported"
    thinking_in_output: bool = True
    temperature_zero: bool = True
    native_tools: bool = True
    note: str = ""

    # ── What a cached token COSTS, relative to a fresh input token ────
    #
    # A cached token occupies the window exactly like a fresh one, so
    # `peak_context` counts it at full weight and must keep doing so.
    # But it is not BILLED like a fresh one, and spend was adding all
    # four fields at parity. With cache coverage now measured at 48% on
    # Anthropic, that is not a rounding error: it overstates the bill by
    # roughly the whole cached half.
    #
    # These are the providers' published multipliers against the base
    # input price, not measurements of ours — the one number here we
    # cannot verify from a run, because the API reports tokens and never
    # prices. They are the reason spend is a TOKEN-EQUIVALENT figure
    # rather than currency: output is left at 1.0 even though it bills
    # several times higher, so this is "input-priced tokens", useful for
    # comparing runs and not a bill.
    cache_read_weight: float = 1.0
    cache_write_weight: float = 1.0

    # Headers the request must carry for the figures above to be true.
    # MEASURED, not assumed: `context-1m-2025-08-07` is ACCEPTED by
    # sonnet-5, opus-5 and haiku-4-5 alike, and only the first two
    # actually get the window — haiku still refuses at
    # "prompt is too long: 242032 tokens > 200000". A header that is
    # accepted and does nothing is exactly how a wrong context window
    # gets into a registry.
    beta_headers: tuple = ()

    # LR7's budget. DECLARED, not derived from the context window.
    #
    # A formula was tempting and wrong: half of Gemini's window is
    # 524,288 against today's 200,000, and adopting it as a side effect
    # of moving capabilities per-model would have made every leg 2.6x
    # longer without anybody choosing that. The existing figures were a
    # decision; this file records them per model so they can be changed
    # per model, which is the whole point.
    leg_token_budget: int = 100_000


def _gemini(model_id: str, max_context: int = 1_048_576,
            max_output: int = 65_536, leg_token_budget: int = 600_000,
            note: str = "") -> ModelSpec:
    return ModelSpec(
        id=model_id, provider="gemini", max_context=max_context,
        max_output=max_output,
        # Gemini reports thinking inside candidates_token_count, so the
        # count is complete and budget_basis stays `measured`.
        reasoning="unreported", thinking_in_output=True,
        temperature_zero=True, leg_token_budget=leg_token_budget,
        # CHECKED AGAINST GOOGLE'S PUBLISHED RATES, 4 September 2026,
        # and the remembered figure was wrong. Across the whole Gemini 3
        # series cached input is a TENTH of input, not a quarter —
        # 3.7-flash $0.075 against $0.75, 3.5-flash $0.15 against $1.50,
        # 3.1-pro $0.20 against $2.00. 0.25 was the Gemini 2.5 ratio,
        # carried forward by hand.
        #
        # And Gemini has no per-token write charge at all: an explicit
        # cache is billed by STORAGE per hour, which a per-token weight
        # cannot express. Zero is right for us because implicit caching
        # stores nothing and reports no write tokens.
        cache_read_weight=0.10, cache_write_weight=0.0,
        note=note)


def _anthropic(model_id: str, max_context: int = 200_000,
               max_output: int = 64_000, leg_token_budget: int = 100_000,
               beta_headers: tuple = (), note: str = "") -> ModelSpec:
    return ModelSpec(
        id=model_id, provider="anthropic", max_context=max_context,
        max_output=max_output,
        # Anthropic reports thinking tokens separately, so they are a
        # real number rather than an estimate.
        reasoning="reported", thinking_in_output=False,
        temperature_zero=True, leg_token_budget=leg_token_budget,
        # Anthropic bills a cache read at 10% of the input rate and a
        # 5-minute cache write at 125%. Writing is dearer than not
        # caching at all, which is why a prefix that is never re-read is
        # a loss and the coverage figure matters.
        cache_read_weight=0.10, cache_write_weight=1.25,
        beta_headers=beta_headers, note=note)


# ── The supported set ─────────────────────────────────────────────────
# Gemini limits read from the provider's models.list() on 4 September
# 2026. Anthropic's context window is the documented 200K standard; the
# 1M variants need a beta header this harness does not send, so claiming
# them here would over-budget every leg.
SUPPORTED: dict[str, ModelSpec] = {m.id: m for m in (
    # Gemini — 3 series and later only.
    _gemini("gemini-3.8-flash"),
    _gemini("gemini-3.7-flash", note="verified at ES3 cycle-09"),
    _gemini("gemini-3.6-flash"),
    _gemini("gemini-3.5-flash"),
    _gemini("gemini-3.5-flash-lite"),
    _gemini("gemini-3.1-pro-preview"),
    _gemini("gemini-3.1-flash-lite"),
    _gemini("gemini-3.1-flash-lite-preview"),
    _gemini("gemini-3-flash-preview"),

    # Anthropic — opus, sonnet and haiku.
    #
    # The 1M window is REAL on opus-5 and sonnet-5 and needs the beta
    # header; measured 4 September 2026 by sending 396,018 input tokens,
    # which both accepted. 200,000 is small for real work — a coding
    # story that reads a large tree spends it on orientation.
    #
    # The leg budget rises with the window but not to half of it.
    #
    # 600,000 ON THE 1M MODELS, chosen and not derived. 200,000 was a
    # figure inherited from the 200K era and kept when the window grew
    # sixfold, so a leg on a 1M model ended having used a fifth of what
    # it could hold -- and every boundary costs 4.6-6.1K to re-orient
    # plus a brief nobody reads twice. It is NOT half the window,
    # because `peak_context` excludes the reply: the real requirement is
    # the budget PLUS `max_output`, and `test_a_leg_budget_leaves_room_
    # for_the_reply` is what keeps that true.
    #
    # THE 200K-WINDOW MODELS ARE UNCHANGED at 100,000. A budget above a
    # model's window is not a bigger leg, it is a predicate that never
    # fires and a provider error instead of a clean boundary.
    _anthropic("claude-opus-5", max_context=1_000_000,
               leg_token_budget=600_000,
               beta_headers=("context-1m-2025-08-07",),
               note="1M window, measured"),
    _anthropic("claude-sonnet-5", max_context=1_000_000,
               leg_token_budget=600_000,
               beta_headers=("context-1m-2025-08-07",),
               note="1M window, measured; verified at ES3 cycle-09"),
    # Haiku ACCEPTS the header and does not get the window — measured,
    # refused at 242,032 tokens. Claiming 1M here would over-budget
    # every leg it runs.
    # Haiku also does not cache a prefix this size: measured with a
    # 3,265-token system+tools prefix and cache_control on the system
    # block, it reported read=0 write=0 on every call while sonnet-5
    # cached 3,941 of the same shape. Cache eligibility is per model,
    # exactly like the 1M window, and assuming otherwise would leave
    # somebody wondering why their haiku runs cost full price.
    _anthropic("claude-haiku-4-5-20251001",
               note="200K; 1M header inert; does not cache a ~3K prefix"),
    # Untested for 1M. Left at the documented standard rather than
    # assumed upward from their siblings.
    _anthropic("claude-opus-4-8", note="200K assumed; 1M untested"),
    _anthropic("claude-sonnet-4-6", note="200K assumed; 1M untested"),

    # The 5.5 pair, added 2026-10-19 so they can be chosen as executor
    # and verifier models.
    #
    # 1M IS THE STANDARD WINDOW HERE, AND NO BETA HEADER IS SENT.
    #
    # Measured twice on 2026-10-19, which is the only reason the second
    # line is right. First with `context-1m-2025-08-07`, as opus-5 and
    # sonnet-5 need: 400,019 tokens accepted. That proved the window
    # and said nothing about the header, so it was measured again
    # WITHOUT it -- 400,015 tokens, also accepted, on both models.
    #
    # So the header is unnecessary on 5.5, and sending it would be
    # cargo copied off the 5-series entries above. `test_the_5_5_
    # request_shape` asserts it is absent; that test is what caught the
    # first version of these two lines carrying it.
    #
    # The measurement matters rather than being ceremony: haiku accepts
    # that same header and does NOT get the window (its note above --
    # refused at 242,032), so neither the presence of a header nor its
    # acceptance tells you anything. Only tokens on the wire do.
    #
    # Budget follows the window at 600,000, as on every 1M model here.
    _anthropic("claude-sonnet-5-5", max_context=1_000_000,
               leg_token_budget=600_000,
               note="1M standard, measured 2026-10-19 at 400,015 "
                    "tokens with no beta header"),
    _anthropic("claude-opus-5-5", max_context=1_000_000,
               leg_token_budget=600_000,
               note="1M standard, measured 2026-10-19 at 400,015 "
                    "tokens with no beta header"),
)}


class UnsupportedModel(Exception):
    """The model cannot be used, and why — never a bare KeyError."""


def _why_refused(model_id: str) -> str | None:
    """A specific reason, when there is one better than 'not in the list'."""
    if any(bad in model_id for bad in NOT_AN_AGENT):
        return (f"{model_id} is not a text-generation model. It will "
                f"accept a request and never call a tool.")
    series = _GEMINI_SERIES.match(model_id)
    if series and int(series.group(1)) < MIN_GEMINI_SERIES:
        return (f"{model_id} is older than the Gemini {MIN_GEMINI_SERIES} "
                f"series, which this harness requires. Measured on "
                f"gemini-2.5-flash: at temperature 0 it returns "
                f"MALFORMED_FUNCTION_CALL for three of the five corpus "
                f"tasks — no parts, no output tokens — because greedy "
                f"decoding takes the same bad token path every time.")
    return None


def resolve(model_id: str) -> ModelSpec:
    """The spec for a model, or a refusal that says what to do instead."""
    spec = SUPPORTED.get(model_id)
    if spec is not None:
        return spec
    reason = _why_refused(model_id)
    if reason:
        raise UnsupportedModel(reason)
    raise UnsupportedModel(
        f"{model_id} is not in the supported set, so its context window "
        f"is unknown — and LR7 derives the leg token budget from it, so "
        f"running it would budget every leg on a guess. Add it to "
        f"registry.SUPPORTED with figures from the provider, or pick "
        f"one of: {', '.join(sorted(SUPPORTED))}")


def provider_of(model_id: str) -> str:
    """Which adapter serves this model. The user picks a model, not a seam."""
    return resolve(model_id).provider


def available(provider: str | None = None) -> list[ModelSpec]:
    return sorted(
        (m for m in SUPPORTED.values()
         if provider is None or m.provider == provider),
        key=lambda m: (m.provider, m.id))


def render() -> str:
    """The menu, for a person choosing. Printed by `./run.sh models`."""
    lines = []
    for provider in ("anthropic", "gemini"):
        lines.append(f"{provider}:")
        for m in available(provider):
            lines.append(
                f"  {m.id:<32} context {m.max_context:>9,}  "
                f"leg budget {m.leg_token_budget:>9,}"
                + (f"  — {m.note}" if m.note else ""))
    return "\n".join(lines)
