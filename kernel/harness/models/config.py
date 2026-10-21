"""Executor model configuration — which model, and how failures are retried.

CONFIGURATION, NOT CONSTANTS. A model id compiled into an adapter's
default argument is a model id nobody can change without a code change,
and it drifted: `BUILD_ORDER.md` has recorded `gemini-3.7-flash` since
ES3 cycle-09 — including a defect found only on it — while the adapter
defaulted to `gemini-2.5-flash`. Every figure Stage G produced was
measured on the wrong model, and nothing anywhere disagreed out loud.

WHAT THAT COST, so the next person does not repeat it: 2.5-flash at
temperature 0 returns `MALFORMED_FUNCTION_CALL` for 3 of the 5 corpus
tasks — no parts, no output tokens — because greedy decoding takes the
same bad token path every time. On 3.7-flash all five are clean. A third
of all model calls in a corpus run were returning nothing, and the cause
was a default argument.

THE RETRY LADDER IS TEMPERATURE, NOT ATTEMPTS. Retrying an identical
request under greedy decoding is deterministic: the same tokens produce
the same malformed call, three times out of three when measured. The
only thing that changes the outcome is changing the sampling, so the
ladder escalates temperature and the attempt count alone is useless.

LR6 CONSTRAINS WHERE THE RETRY LIVES: *retries are journalled steps,
never internal — an adapter that retries silently hides spend from the
budget predicate and stalling from the progress clock.* So this module
carries the POLICY and the loop applies it. An adapter that retried on
its own would be spending tokens the budget cannot see.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# S10: no `environ.get` with a default. A configuration value that
# silently falls back is a configuration value nobody knows is unset —
# which is the entire failure this module exists to end. The default
# lives HERE, in the open, and the environment overrides it explicitly.
DEFAULT_MODELS = {
    "gemini": "gemini-3.7-flash",
    "anthropic": "claude-sonnet-5",
}

_ENV_MODEL = "AGENTOS_MODEL"
_ENV_RETRY = "AGENTOS_RETRY_POLICY"
_ENV_LEG = "AGENTOS_LEG_BUDGET"
_ENV_CACHE = "AGENTOS_CACHE"

# WHERE EACH PROVIDER'S CREDENTIAL COMES FROM. One place, because two
# adapters reading their own variable name is two chances to disagree
# with the compose file.
_ENV_KEY = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def api_key(provider: str) -> str:
    """The credential for one provider, refusing an empty one.

    AN EMPTY KEY IS NOT A KEY, and treating it as one is the difference
    between an error that names the profile and an error that names the
    vendor. `docker-compose.yml` writes `${ANTHROPIC_API_KEY:-}`, so an
    unset variable arrives as the empty string rather than absent — the
    adapter then constructs happily and the leg dies several seconds
    later on a 401, which `faults.py` classifies AUTH and does not retry.

    That matters more now that a profile can name its own model. An
    errand profile asking for `claude-sonnet-5` on a stack with no
    Anthropic key should fail at `adapter_for_profile`, saying which
    profile and which variable — not mid-leg, in the provider's words.
    """
    name = _ENV_KEY.get(provider)
    if name is None:
        raise ValueError(
            f"no credential is defined for provider {provider!r}; "
            f"known: {', '.join(sorted(_ENV_KEY))}")
    value = (os.environ[name] if name in os.environ else "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is unset or empty, so the {provider!r} adapter cannot "
            f"be built. Every executor may name a model from either "
            f"provider, so both keys belong in the worker's environment.")
    return value


@dataclass(frozen=True)
class RetryPolicy:
    """How a failed model call is retried. Every field is configuration.

    `temperature_ladder` is the load-bearing one. Attempts at a fixed
    temperature cannot recover a malformed tool call from a greedy
    decoder, because the decode is deterministic — the ladder is what
    changes the sampling path. The first entry is the normal operating
    temperature, so a run that never fails never leaves it.
    """

    # Applied to a malformed tool call: try 0, then 0.3, then 0.7.
    temperature_ladder: tuple = (0.0, 0.3, 0.7)

    # Transport and rate-limit failures, which ARE worth repeating
    # unchanged — unlike a malformed call, nothing about the request is
    # wrong.
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    # Full jitter. Without it a fleet that failed together retries
    # together and reproduces the overload it is backing off from.
    jitter: bool = True
    # Honour the provider's own answer over our arithmetic when it gives
    # one — a 429 that says 20 seconds means 20 seconds.
    honour_retry_after: bool = True

    # The circuit breaker. `MAX_LEGS` is 12 and the worker tick is 4s, so
    # without this every running story burns its ceiling in ~48 seconds
    # of outage and lands in FAILED — a ceiling meant to stop a run
    # flailing, spent punishing it for somebody else's downtime.
    breaker_enabled: bool = True
    breaker_failures: int = 5          # correlated failures to trip
    breaker_window_s: float = 60.0
    breaker_probe_s: float = 30.0      # how often to test the water

    # How many CONSECUTIVE empty responses end the leg. Empty steps do
    # not consume the step budget — a provider fact must not spend the
    # run's ceiling — so something else has to bound them or a provider
    # returning nothing is an infinite leg. -1 is unbounded.
    empty_ceiling: int = 6

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before attempt `attempt` (1-based)."""
        if retry_after is not None and self.honour_retry_after:
            return max(0.0, float(retry_after))
        delay = min(self.base_delay_s * (2 ** max(0, attempt - 1)),
                    self.max_delay_s)
        if self.jitter:
            # Full jitter rather than a fraction: it is the variant that
            # actually decorrelates, and the worst case is one fast retry
            # rather than a synchronised herd.
            import random
            delay = random.uniform(0.0, delay)
        return delay

    def temperature_for(self, attempt: int) -> float:
        """Sampling temperature for attempt `attempt` (0-based)."""
        ladder = self.temperature_ladder or (0.0,)
        return float(ladder[min(attempt, len(ladder) - 1)])


@dataclass(frozen=True)
class ModelConfig:
    model: str
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def _policy_from_env() -> RetryPolicy:
    """`AGENTOS_RETRY_POLICY` as JSON, overriding field by field.

    Field-by-field rather than all-or-nothing: a deployment that wants a
    longer breaker window should not have to restate the ladder and
    thereby freeze it at today's values.
    """
    if _ENV_RETRY not in os.environ:
        return RetryPolicy()
    raw = json.loads(os.environ[_ENV_RETRY])
    known = {f for f in RetryPolicy.__dataclass_fields__}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"{_ENV_RETRY}: unknown field(s) {sorted(unknown)}. Known: "
            f"{sorted(known)}. A typo that is silently ignored is a "
            f"policy nobody is running.")
    if "temperature_ladder" in raw:
        raw["temperature_ladder"] = tuple(raw["temperature_ladder"])
    return RetryPolicy(**{**RetryPolicy().__dict__, **raw})


@dataclass(frozen=True)
class CachePolicy:
    """Prompt caching. Configuration, and on by default.

    OPT-OUT, NOT OPT-IN, which is upstream OpenClaw's choice and the
    right one: §8.3 puts input replay at ~80% of a large story's spend,
    so a caching default of `none` is a default that costs money for no
    reason. Anything that genuinely should not cache — a one-shot
    errand, a bursty notifier — says so.

    `short` and `long` mirror the two TTLs both providers offer, rather
    than inventing a third vocabulary: 5 minutes and 1 hour.

    THE TWO PROVIDERS CACHE THE SAME SCOPE BY DIFFERENT MEANS, and the
    scope is `system instruction + tool declarations` — the part that is
    byte-identical on every step of every leg. Not the conversation:
    it grows every step, so re-caching it costs more than it saves on
    Gemini, where a cache is a created resource. Anthropic could go
    further with a moving breakpoint; that is measured before it is
    built, not assumed.
    """

    retention: str = "short"           # none | short | long

    # CREATE A PROVIDER-SIDE STORED CACHE? Gemini only, and OFF.
    #
    # Gemini's `cachedContents` is a resource we create and Google keeps
    # until its TTL expires. Ours never got created: the system prompt
    # plus tool declarations comes to 245-815 tokens and Google's
    # minimum is 1,024, so every call was refused as "Cached content is
    # too small" and the backoff quietly absorbed it. Verified against
    # the live account — `caches.list()` returns zero, so nothing is or
    # was being stored.
    #
    # Meanwhile Gemini's IMPLICIT caching, which needs no resource and
    # stores nothing on our behalf, measured 28-29% coverage across
    # 118 of 125 steps. It is doing the job the explicit path was built
    # for, at no storage and no lifecycle.
    #
    # So: implicit only. Turning this on without first padding the
    # prefix past 1,024 tokens just reinstates the refusals.
    stored_cache: bool = False
    # Recreate a cache this long before it expires, so a step never
    # lands in the gap between expiry and the next create.
    refresh_margin_s: float = 30.0
    # A provider that refuses to create a cache must not be asked again
    # on the next step. Ten minutes, from upstream's figure.
    failure_backoff_s: float = 600.0

    TTLS = {"none": 0, "short": 300, "long": 3600}

    def __post_init__(self):
        if self.retention not in self.TTLS:
            raise ValueError(
                f"cache retention {self.retention!r}; expected one of "
                f"{sorted(self.TTLS)}")

    @property
    def enabled(self) -> bool:
        return self.retention != "none"

    @property
    def ttl_s(self) -> int:
        return self.TTLS[self.retention]


def cache_policy() -> CachePolicy:
    """The policy, with `AGENTOS_CACHE` as the user's hand on the dial."""
    if _ENV_CACHE not in os.environ:
        return CachePolicy()
    raw = os.environ[_ENV_CACHE].strip()
    # A bare word is the common case and should not require JSON.
    if raw in CachePolicy.TTLS:
        return CachePolicy(retention=raw)
    parsed = json.loads(raw)
    known = {"retention", "refresh_margin_s", "failure_backoff_s"}
    unknown = set(parsed) - known
    if unknown:
        raise ValueError(
            f"{_ENV_CACHE}: unknown field(s) {sorted(unknown)}; known "
            f"{sorted(known)}")
    return CachePolicy(**{**CachePolicy().__dict__, **parsed})


@dataclass(frozen=True)
class LegBudget:
    """How big a leg is. Chosen by the user, not compiled into a signature.

    `step_budget: int = 200` was a default argument and `MAX_LEGS = 12` a
    module constant — the same shape as the model id that turned out to
    be the wrong model for the whole of Stage G. A number nobody can
    change without editing code is a number nobody revisits.

    THE THREE INTERACT, and getting that wrong is the easy mistake:

        tokens    ends a leg when the model has spent enough
        steps     ends a leg when it has taken enough turns
        max_legs  ends the STORY, as FAILED

    Halving `tokens` does not halve cost — it doubles the number of legs,
    and every boundary costs E5c's 4.6–6.1K to re-orient. Halve it far
    enough and a story that used to finish in five legs needs more than
    `max_legs` and fails for a reason that has nothing to do with the
    work. So a small leg wants a larger ceiling, and `for_testing()`
    raises it rather than leaving the trap set.
    """

    # TWO BUDGETS, BECAUSE THERE ARE TWO QUESTIONS.
    #
    # This was one field, `tokens`, summed across every step of the leg
    # and sized from the model's context window. Those are different
    # measurements and the gap is not small — measured on a real leg:
    #
    #     22 steps   summed spend 190,719   peak context 17,397
    #
    # The leg ended on a 200,000 budget having never used 1.7% of
    # Gemini's window. Summing counts the same tokens repeatedly, because
    # step N's input CONTAINS steps 1..N-1. That is exactly right for
    # billed cost — the provider charges for every token in every
    # request — and exactly wrong for context, where what matters is the
    # largest single request.
    #
    # A leg boundary exists to manage CONTEXT: stop, write a brief,
    # resume before the window fills. So context is what ends a leg, and
    # spend is a separate cost ceiling that has nothing to do with the
    # window.
    #
    # Getting this wrong cost real money and real time. Legs ended about
    # sixty times earlier than the window required, and every premature
    # boundary pays E5c's 4.6-6.1K to re-orient.
    #
    # -1 ON ANY OF THESE MEANS UNBOUNDED — run until the work is done.
    # A large number cannot express "no ceiling": somebody reading 999999
    # cannot tell whether it was chosen or given up on.
    #
    # What still stops a runaway with all of them unbounded: the story's
    # own completion, a cancel from a person, the tick ceiling in
    # whatever drives the loop, and the provider's bill. Nothing else.

    # Ends the leg. The PEAK of `input + cacheRead` on any single step —
    # how much the model actually had to hold at once.
    context_tokens: int = 120_000
    # A cost ceiling, cumulative across the leg. Deliberately generous:
    # it is a backstop against a runaway, not the thing that paces a leg.
    # 6,000,000, RAISED WITH THE CONTEXT BUDGET AND FOR THE SAME REASON.
    #
    # This is the COST backstop, not the window: it catches a leg that is
    # cheap per step and grinding, so it only means anything while it
    # sits well above what an honest leg spends. A 600,000-token leg on
    # a 1M model reaches a peak six times the old one and its cumulative
    # spend rises with it -- each step re-sends the conversation -- so
    # 2,000,000 would have started ending healthy legs for a reason that
    # reads as a cost problem and is not one.
    #
    # NOT SETTABLE PER PROFILE, deliberately: `resolve_budget` builds a
    # profile budget from `leg_tokens`/`leg_steps`/`max_legs` and takes
    # this field from here every time. A per-agent cost ceiling is a
    # different decision from a per-agent leg size, and nobody has asked
    # for one.
    spend_tokens: int = 6_000_000
    steps: int = 200
    max_legs: int = 12

    UNBOUNDED = -1

    @staticmethod
    def unbounded(value: int) -> bool:
        return value == LegBudget.UNBOUNDED

    def __post_init__(self):
        for field_name in ("context_tokens", "spend_tokens", "steps",
                           "max_legs"):
            value = getattr(self, field_name)
            if value != self.UNBOUNDED and value <= 0:
                raise ValueError(
                    f"leg budget {field_name}={value}: use a positive "
                    f"number, or -1 for unbounded. 0 is neither — it "
                    f"would end the run before it started.")

    @classmethod
    def for_testing(cls) -> "LegBudget":
        """Smaller legs, so a suite crosses a boundary and costs less.

        A small CONTEXT budget is a real knob in a way a small spend
        budget never was. 30,000 tokens of context is tens of steps of
        genuine work; 30,000 of cumulative spend was about five, all of
        them orientation, and the run never finished.
        """
        return cls(context_tokens=30_000, max_legs=24)


def choose_leg(explicit: "LegBudget | None" = None,
               contract: dict | None = None,
               profile_budget: "LegBudget | None" = None,
               model_tokens: int | None = None) -> LegBudget:
    """Leg size for this run. Most specific wins, same order as the model.

        explicit          a caller that means it
        contract          THE USER'S CHOICE for this run
        profile_budget    what this agent type is built around
        model_tokens      LR7's per-model figure, as the floor

    Field-by-field rather than all-or-nothing: a user who wants shorter
    legs should not have to restate `max_legs` and thereby freeze it.
    """
    base = LegBudget()
    if model_tokens:
        base = LegBudget(context_tokens=model_tokens, steps=base.steps,
                         spend_tokens=base.spend_tokens,
                         max_legs=base.max_legs)
    if profile_budget:
        base = profile_budget
    if contract:
        known = {"context_tokens", "spend_tokens", "steps",
                 "max_legs"}
        unknown = set(contract) - known
        if unknown:
            raise ValueError(
                f"leg budget: unknown field(s) {sorted(unknown)}; known "
                f"{sorted(known)}. A typo silently ignored is a budget "
                f"nobody is running.")
        base = LegBudget(**{**base.__dict__, **contract})
    # The environment is the user's hand on the dial, and it sits ABOVE
    # the profile for the same reason `AGENTOS_MODEL` does: the person
    # running this is more specific than the agent type they picked.
    if _ENV_LEG in os.environ:
        raw = json.loads(os.environ[_ENV_LEG])
        known = {"context_tokens", "spend_tokens", "steps",
                 "max_legs"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                f"{_ENV_LEG}: unknown field(s) {sorted(unknown)}; known "
                f"{sorted(known)}")
        base = LegBudget(**{**base.__dict__, **raw})
    return explicit or base


def choose(explicit: str | None = None,
           contract_model: str | None = None,
           profile_model: str | None = None,
           default: str | None = None) -> str:
    """Which model this run uses. Most specific wins.

        explicit          a caller naming one — a conformance suite
                          pinning two models at once means it
        contract_model    THE USER'S CHOICE for this run
        profile_model     what this agent type is built around
        default           the fallback, declared in the open

    A user picks a MODEL, not a provider: the registry says which
    adapter serves it. Asking somebody to name both is asking them to
    know which seam we happen to have, and to keep the two in step.

    Every candidate is resolved against the registry, so an unusable
    model is refused HERE — at selection, with the list — rather than
    at the first model call of a leg that has already claimed a slot,
    built a sandbox and cloned a repository.
    """
    from kernel.harness.models.registry import resolve

    for candidate in (explicit, contract_model, profile_model, default):
        if candidate:
            resolve(candidate)          # refuses with a reason
            return candidate
    raise ValueError(
        "no model chosen and no default — pass one, set it on the "
        "contract or the profile, or set AGENTOS_MODEL")


def provider_for(model: str) -> str:
    """The adapter that serves this model."""
    from kernel.harness.models.registry import provider_of

    return provider_of(model)


def for_adapter(name: str, model: str | None = None) -> ModelConfig:
    """The configuration for one adapter. Explicit beats ambient.

    Precedence: the caller's argument, then `AGENTOS_MODEL`, then the
    default above. A caller that names a model means it — that is how a
    conformance suite pins two models at once.
    """
    env_model = os.environ[_ENV_MODEL] if _ENV_MODEL in os.environ else None
    if name not in DEFAULT_MODELS and model is None and env_model is None:
        raise ValueError(
            f"no default model for adapter {name!r}; pass one or set "
            f"{_ENV_MODEL}")
    chosen = choose(explicit=model, contract_model=env_model,
                    default=DEFAULT_MODELS.get(name))
    # A model that belongs to the OTHER provider is a mistake worth
    # catching here: it would otherwise be sent to an SDK that has never
    # heard of it, and come back as a vendor 404.
    from kernel.harness.models.registry import provider_of

    actual = provider_of(chosen)
    if actual != name:
        raise ValueError(
            f"{chosen} is served by the {actual!r} adapter, not "
            f"{name!r}. Ask for the model and let the registry pick the "
            f"seam.")
    return ModelConfig(model=chosen, retry=_policy_from_env())
