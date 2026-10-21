"""H1b — where an executor's configuration comes from.

S8 fixed the SHAPE: configuration flows down at dispatch rather than being
baked into the image. It did not fix the SOURCE, which was still the
Conductor's own environment filtered through an allowlist. Two problems
with that, and only the second is about security:

  * an allowlist cannot express a PER-STORY value. Every execution the
    Conductor dispatches gets the same numbers, so a contract that wants
    a smaller budget, a different image or a narrower tool set has no way
    to say so — and `contracts.budget` exists precisely to say it.
  * the Conductor holds DATABASE_URL and the API keys. An allowlist is a
    permit list and `assert_no_credentials` guards it, but the guard
    exists because the SOURCE is wrong: nothing that reads from the
    Conductor's environment can be safe by construction, only by
    vigilance.

WHAT THIS DOES NOT DO. The allowlist is not deleted. A run with no
contract — an errand, or a story before its contract is approved — still
needs an image and still gets one, so the allowlist remains the FALLBACK
and the contract is the override. Deleting it would make dispatch depend
on a row that does not exist yet.

THE ADMISSION CHECK, and how it changed. This increment was to refuse a
contract whose per-leg budget sits below the measured orientation floor,
with the floor quoted in the refusal — a budget too small to reach a
first write is a run that orients and dies, and a per-contract budget is
the first thing in the design that can be set too small on purpose.

The floor was deliberately not measured (decision, 4 September 2026:
production runs a generous leg size, which settles the question by being
far above it). So the check derives from the CONFIGURED leg size instead:
a contract may not ask for less context than the harness's own floor
constant. Weaker, and it still catches the case it was built for —
somebody typing 5000 into a budget field.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The smallest per-leg context a contract may ask for. NOT a measured
# orientation floor — see the module docstring. It is a floor on the
# floor: below this a leg cannot orient at all, and the failure looks
# like a model that would not work.
MIN_CONTEXT_TOKENS = 20_000


class ContractRefused(Exception):
    """Refused at admission, with the reason and the number in it.

    Raised rather than logged: a contract that cannot produce a working
    run must not produce a container, and discovering it later means a
    leg that orients and never reaches a write.
    """


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def check_budget(budget) -> dict:
    """Validate a contract's budget. Returns it, or refuses by name."""
    spec = _as_dict(budget)
    context = spec.get("context_tokens")
    if context is None:
        return spec
    if not isinstance(context, int) or (context < 0 and context != -1):
        raise ContractRefused(
            f"contract budget context_tokens must be a positive integer or "
            f"-1 for unbounded, got {context!r}")
    if context != -1 and context < MIN_CONTEXT_TOKENS:
        raise ContractRefused(
            f"contract asks for {context:,} context tokens per leg; the "
            f"minimum this harness will dispatch is {MIN_CONTEXT_TOKENS:,}. "
            f"A leg below that cannot orient — it reads, searches, runs out "
            f"and writes nothing — and the run then looks like a model that "
            f"would not work rather than a budget that was set too small.")
    return spec


def overrides_from(contract_row) -> dict:
    """What this contract says about how its executions run.

    Only keys the contract actually sets are returned, so a caller can
    tell "the contract chose this" from "nobody said". An absent contract
    yields {} and the allowlist path is unchanged.
    """
    if contract_row is None:
        return {}
    out: dict = {}
    image = contract_row.get("base_digest") or contract_row.get("base_image")
    if image:
        # DIGEST BEFORE TAG. F6's parity check exists because a contract
        # naming an image by tag may not be the image the Verifier ran;
        # when the contract carries a digest, dispatch uses it.
        out["image"] = image
    budget = check_budget(contract_row.get("budget"))
    if budget:
        out["budget"] = budget
    # I7 -- a contract may ask for a bigger rung than its profile's. The
    # profile says what this KIND of work usually needs; a contract knows
    # about the particular repository, and a monorepo whose test suite
    # needs 8 GiB is a fact about that repo rather than about stories.
    # Resolved here so an unknown word is refused at dispatch with the
    # ladder named, not silently rounded down to the default.
    size = (contract_row.get("size") or "").strip()
    if size:
        from kernel.sizes import resolve as _resolve_size  # noqa: PLC0415

        out["size"] = _resolve_size(size).name
    return out
