"""Unit tests for `availability.py` — the single source of truth for
provider display precedence + model selectability, shared by the admin
badge, the three dropdown builders, and `is_model_registered_v2`.
"""
from __future__ import annotations

from app.services.model_registry.availability import (
    effective_provider_status,
    is_model_selectable,
    model_credential_warning,
)


def _provider(**over) -> dict:
    row = {
        "runtime_status": "provisioned",
        "status": "active",
        "credential_status": "healthy",
        "credential_error": None,
    }
    row.update(over)
    return row


# ===========================================================================
# effective_provider_status — precedence order.
# ===========================================================================


def test_not_provisioned_wins_over_everything():
    assert effective_provider_status(_provider(
        runtime_status="not_provisioned", status="disabled",
        credential_status="revoked",
    )) == "not_provisioned"


def test_disabled_wins_over_credential_state():
    assert effective_provider_status(_provider(
        status="disabled", credential_status="revoked",
    )) == "disabled"


def test_revoked_shown_when_provisioned_and_active():
    assert effective_provider_status(_provider(credential_status="revoked")) == "revoked"


def test_unhealthy_shown_when_provisioned_and_active():
    assert effective_provider_status(_provider(credential_status="unhealthy")) == "unhealthy"


def test_healthy_shown_for_fully_working_provider():
    assert effective_provider_status(_provider()) == "healthy"


def test_active_fallback_when_no_credential_wired_up():
    """e.g. claude-cli — no secret_id at all, credential_status stays
    'unknown' forever since nothing is ever resolved for it."""
    assert effective_provider_status(_provider(credential_status="unknown")) == "active"


# ===========================================================================
# is_model_selectable — hard-exclude only on status='disabled'.
# ===========================================================================


def test_selectable_true_for_healthy_provider():
    assert is_model_selectable(_provider()) is True


def test_selectable_false_for_disabled_provider():
    assert is_model_selectable(_provider(status="disabled")) is False


def test_selectable_true_for_not_provisioned_provider():
    """The load-bearing regression test: runtime_status must NEVER gate
    execution/selectability (management.py's `_RUNTIME_STATUSES`
    comment) — this must stay True even though not_provisioned is the
    highest-precedence DISPLAY state."""
    assert is_model_selectable(_provider(runtime_status="not_provisioned")) is True


def test_selectable_true_for_revoked_credential():
    """Soft signal only — a revoked credential does not hard-exclude,
    it surfaces via model_credential_warning instead."""
    assert is_model_selectable(_provider(credential_status="revoked")) is True


def test_selectable_true_for_unhealthy_credential():
    assert is_model_selectable(_provider(credential_status="unhealthy")) is True


def test_selectable_true_for_unknown_credential():
    """A brand-new provider mid-way through its first 5-minute health
    check must not be hidden just because nothing has resolved yet."""
    assert is_model_selectable(_provider(credential_status="unknown")) is True


# ===========================================================================
# model_credential_warning
# ===========================================================================


def test_no_warning_for_healthy_credential():
    assert model_credential_warning(_provider()) is None


def test_no_warning_for_unknown_credential():
    assert model_credential_warning(_provider(credential_status="unknown")) is None


def test_warning_for_revoked_credential():
    warning = model_credential_warning(_provider(credential_status="revoked"))
    assert warning is not None
    assert "revoked" in warning


def test_warning_for_unhealthy_credential_includes_error_message():
    warning = model_credential_warning(_provider(
        credential_status="unhealthy", credential_error="permission denied",
    ))
    assert warning == "This provider's credential is unhealthy: permission denied — requests may fail."


def test_warning_for_unhealthy_credential_without_error_message():
    warning = model_credential_warning(_provider(
        credential_status="unhealthy", credential_error=None,
    ))
    assert warning == "This provider's credential is unhealthy — requests may fail."
