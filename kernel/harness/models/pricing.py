"""What a step actually cost, in money, at the provider's own rates.

RATES ARE DATA, NOT CONSTANTS. They are not in this file, and that is
deliberate twice over.

They change without any code change — a provider re-prices and every
hardcoded figure in the tree is silently wrong, still reporting to eight
decimal places. And they are the one quantity in this harness that
cannot be verified from a run: the APIs return token counts and never
prices, so a wrong rate here produces a confident number that no test,
no reconciliation and no amount of running can contradict. Everything
else in the accounting was caught by comparing against the provider.
This cannot be, so it must not be guessed.

The platform reached the same conclusion independently: its
`model_pricing` table carries input_rate, output_rate, cache_read_rate,
cache_write_rate and thinking_rate as operator-entered rows keyed by
(provider, model, effective_from), append-only and time-aware, with no
seed data in any migration. The column names here are that table's, so
rates move between the two without translation.

WHY THIS EXISTS AT ALL, given `_sum_spend` already returns a number:
that number is token-equivalents at the input rate. It weights cache
reads correctly relative to input but leaves output at 1.0, and output
bills several times higher — so it is honest for comparing two runs of
the same shape and wrong for anything shaped differently. Four separate
rates, one per field, is what the providers actually publish.

    ./run.sh check-tokens        reports money when rates are configured,
                                 and says plainly when they are not
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Rates are PER MILLION TOKENS, in USD — the unit providers publish in,
# and the unit the platform's model_pricing table stores: every one of
# its cost views computes `tokens * rate / 1000000.0`. Storing per-token
# here would have made rows look interchangeable between the two systems
# while being a millionfold apart, which is the kind of error that
# reports a $30 run as $0.00003 and looks merely small.
PER_MILLION = 1_000_000.0
_ENV = "AGENTOS_PRICING"
_DEFAULT_PATH = "/app/pricing.json"

_FIELDS = ("input_rate", "output_rate", "cache_read_rate",
           "cache_write_rate", "thinking_rate")


class PricingError(Exception):
    """The rates file exists and is wrong. Silence here would be worse."""


@dataclass(frozen=True)
class ModelRates:
    input_rate: float
    output_rate: float
    cache_read_rate: float
    cache_write_rate: float
    thinking_rate: Optional[float] = None


_CACHE: Optional[dict] = None


def _load() -> dict:
    """Rates by model id. Missing file is empty, not an error.

    A harness with no rates configured must still run — costing is a
    report, not a dependency. A MALFORMED file is a different matter and
    raises: it means somebody meant to configure rates and the numbers
    are not being applied, which is exactly the failure that would
    otherwise show up as a quietly free run.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = (os.environ[_ENV] if _ENV in os.environ else _DEFAULT_PATH)
    if not os.path.exists(path):
        _CACHE = {}
        return _CACHE
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as e:                                  # noqa: BLE001
        raise PricingError(f"{path} is not readable JSON: {e}") from e

    out: dict = {}
    for model, entry in (raw.get("models") or {}).items():
        # A model listed with nulls is a placeholder someone has not
        # filled in yet. Skipping it reports "no rates" rather than
        # costing the run at zero, which would read as free.
        if not isinstance(entry, dict) or any(
                entry.get(f) is None for f in _FIELDS[:4]):
            continue
        unknown = set(entry) - set(_FIELDS) - {"note"}
        if unknown:
            raise PricingError(
                f"{path}: model {model!r} has unknown field(s) "
                f"{sorted(unknown)}; expected {list(_FIELDS)}")
        try:
            out[model] = ModelRates(
                **{f: (float(entry[f]) if entry.get(f) is not None else None)
                   for f in _FIELDS})   # `note` is documentation, not a rate
        except (TypeError, ValueError) as e:
            raise PricingError(
                f"{path}: model {model!r} has a non-numeric rate: {e}") from e
    _CACHE = out
    return _CACHE


def forget() -> None:
    """For tests. Rates cached across tests are a shared fixture."""
    global _CACHE
    _CACHE = None


def rates_for(model: Optional[str]) -> Optional[ModelRates]:
    if not model:
        return None
    return _load().get(model)


def step_cost_usd(cost: Optional[dict]) -> Optional[float]:
    """Money for one step in USD, or None when this model has no rates.

    None is not zero and callers must not treat it as zero — an
    unconfigured model reporting $0.00 is the failure this returns None
    to avoid.

    Thinking is a SUBSET of output, so it is subtracted before output
    is priced and charged at its own rate — the platform's formula,
    reproduced so identical rows give identical answers in both systems.
    """
    if not cost:
        return None
    rates = rates_for(cost.get("model"))
    if rates is None:
        return None
    # THINKING SPLIT OUT OF OUTPUT, matching the platform's formula
    # exactly: (output - thinking) at output_rate, thinking at
    # thinking_rate falling back to output_rate. With no thinking_rate
    # set the two terms collapse to output * output_rate, so this costs
    # the same as the simple form until a provider bills thinking
    # apart — and then it is already right, rather than quietly
    # under-reporting the dearest tokens in the turn.
    output = cost.get("output") or 0
    thinking = min(cost.get("thinking") or 0, output)
    thinking_rate = (rates.thinking_rate if rates.thinking_rate is not None
                     else rates.output_rate)
    return (
        (cost.get("input") or 0) * rates.input_rate
        + (output - thinking) * rates.output_rate
        + thinking * thinking_rate
        + (cost.get("cacheRead") or 0) * rates.cache_read_rate
        + (cost.get("cacheWrite") or 0) * rates.cache_write_rate
    ) / PER_MILLION


def cache_ratios(model: Optional[str]) -> Optional[tuple]:
    """(read, write) as multiples of the input rate, from real rates.

    The registry carries the same two ratios as published fallbacks so
    the budget backstop works with no rates configured. When rates ARE
    configured they are the better source, being the actual numbers
    rather than a remembered ratio, and one source beats two that can
    disagree.
    """
    rates = rates_for(model)
    if rates is None or not rates.input_rate:
        return None
    return (rates.cache_read_rate / rates.input_rate,
            rates.cache_write_rate / rates.input_rate)
