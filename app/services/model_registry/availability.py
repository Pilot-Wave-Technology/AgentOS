"""Single source of truth for "is this model actually usable right now,"
derived from a model row + its owning provider row.

Three independent status dimensions live on `model_providers`:
  - `runtime_status` ('provisioned' / 'not_provisioned') — infra
    bookkeeping, unrelated to credentials.
  - `status` ('active' / 'disabled') — admin-toggled kill switch for the
    whole provider.
  - `credential_status` ('unknown' / 'healthy' / 'unhealthy' / 'revoked')
    — gateway-reported credential health, except 'revoked' which is
    admin-asserted and sticky (see `management.py`'s
    `set_provider_credential_status_v2` docstring for why the periodic
    gateway health report must never overwrite it).

These three fields stay separate in storage — collapsing them into one
column would lose real, independent information (a provider can be
genuinely `not_provisioned` while still `status='active'`, meaning
"intended for use, just not wired up yet", a different fact from
`status='disabled'`, "turned off on purpose"). What DOES need to be
unified is what's DISPLAYED and what's SELECTABLE — every consumer
(the admin badge, the three model-picker dropdowns, and
`is_model_registered_v2`'s write-path validation used by
`change_agent_model`/`create_goal`) must agree on both, or one surface
could show/allow a model another surface would refuse. This module is
that single point — any future fix to the precedence or filtering rule
lands here once, not once per caller.
"""
from __future__ import annotations

from typing import Any

# Precedence order for the single displayed provider status, most
# severe/relevant first. A provider can legitimately be
# `not_provisioned` AND `disabled` AND `revoked` all in storage — only
# the highest-precedence one is ever shown, so the admin page never
# renders a combination that reads as contradictory (e.g. "Active" next
# to "Not provisioned").
def effective_provider_status(provider: dict[str, Any]) -> str:
    """One of: 'not_provisioned', 'disabled', 'revoked', 'unhealthy',
    'healthy' (credential resolved and reported healthy), or 'active'
    (no credential wired up at all yet — e.g. claude-cli, or a native
    provider with no `secret_id` set)."""
    if provider.get("runtime_status") == "not_provisioned":
        return "not_provisioned"
    if provider.get("status") == "disabled":
        return "disabled"
    credential_status = provider.get("credential_status")
    if credential_status == "revoked":
        return "revoked"
    if credential_status == "unhealthy":
        return "unhealthy"
    if credential_status == "healthy":
        return "healthy"
    return "active"


# Statuses that make a provider's models hard-unselectable everywhere
# a model can be picked or assigned.
#
# Deliberately NOT including 'not_provisioned' here, even though it's
# in `effective_provider_status`'s precedence above it. `runtime_status`
# is explicitly documented (management.py, `_RUNTIME_STATUSES`) as
# "inventory bookkeeping only — this field does NOT gate execution
# anywhere in the app. Anyone consuming it as an execution gate is a
# bug." Using it here would violate that standing rule — it can
# describe/display as "not provisioned" without removing the model
# from anything selectable.
#
# Also NOT including 'revoked'/'unhealthy'/'unknown' credential states
# — those are gateway-reported and can be stale or transiently wrong (a
# brand-new provider is 'unknown' for up to 5 minutes before its first
# health check), so hiding a model outright on that basis risks hiding
# a perfectly usable one. Hard-exclude only on the one fact we're
# certain of: an admin explicitly disabled the provider.
_HARD_UNSELECTABLE_STATUSES = frozenset({"disabled"})


def is_model_selectable(provider: dict[str, Any]) -> bool:
    """False if `provider` (the model's owning `model_providers` row)
    has been explicitly disabled by an admin. Takes the provider row,
    not the model row — model-level `status` ('active'/'disabled') is
    a separate, already-enforced filter upstream
    (`list_base_models_v2(include_disabled=False)`); this function only
    adds the provider-level check that was previously missing entirely.
    Deliberately does NOT exclude on `runtime_status` — see
    `_HARD_UNSELECTABLE_STATUSES`'s comment for why."""
    return effective_provider_status(provider) not in _HARD_UNSELECTABLE_STATUSES


def model_credential_warning(provider: dict[str, Any]) -> str | None:
    """Human-readable warning to surface next to a still-selectable
    model whose provider's credential is in a suspect state — 'revoked'
    or 'unhealthy'. Returns None when there's nothing to warn about.
    Soft signal only (see module docstring for why these aren't
    hard-excluded): the actual failure, if any, still surfaces clearly
    at request time regardless of whether this warning was shown."""
    credential_status = provider.get("credential_status")
    if credential_status == "revoked":
        return "This provider's credential has been marked revoked — requests may fail."
    if credential_status == "unhealthy":
        error = provider.get("credential_error")
        return f"This provider's credential is unhealthy{f': {error}' if error else ''} — requests may fail."
    return None
