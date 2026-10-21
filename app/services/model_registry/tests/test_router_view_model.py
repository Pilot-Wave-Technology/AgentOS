"""Unit tests for `_group_hierarchy_v2` in `app/routers/model_registry.py`.

The helper is pure Python (no DB), so these tests exercise it directly
with hand-rolled `list_providers_v2` + `list_base_models_v2` output
shapes. Verifies: ordering by provider id then model id, one entry
per provider, models correctly nested under their provider by
provider_id, and `synthesized_variants` pre-expanded via
`synthesize_all_variants` on every model row.

Cycle-01 of story 414f92d6.
"""
from __future__ import annotations

from app.routers.model_registry import _group_hierarchy_v2


# ---------------------------------------------------------------------------
# DRY helpers.
# ---------------------------------------------------------------------------


def _provider(id_, **overrides):
    row = {
        "id": id_,
        "label": id_.title(),
        "base_url": None,
        "api_key_source": None,
        "api_type": None,
        "runtime_status": "provisioned",
        "status": "active",
    }
    row.update(overrides)
    return row


def _model(id_, provider_id, **overrides):
    row = {
        "id": id_,
        "provider_id": provider_id,
        "model_name": id_.split("/", 1)[1],
        "wire_id": id_,
        "label": id_.split("/")[-1].replace("-", " ").title(),
        "tier": "fast",
        "context_window": 200_000,
        "provider_shorthand": None,
        "available_context_windows": [],
        "status": "active",
        "created_at": "t0",
        "updated_at": "t0",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Shape + ordering.
# ---------------------------------------------------------------------------


def test_empty_inputs_produce_empty_output():
    assert _group_hierarchy_v2([], []) == []


def test_providers_sorted_by_id_ascending():
    """Even if the DB read returned them out of alphabetical order, the
    view-model must be lex-sorted by provider id so the admin page is
    stable across renders."""
    providers = [_provider("google"), _provider("anthropic"), _provider("openai")]
    grouped = _group_hierarchy_v2(providers, [])
    assert [p["id"] for p in grouped] == ["anthropic", "google", "openai"]


def test_models_sorted_by_id_within_each_provider():
    providers = [_provider("google")]
    models = [
        _model("google/gemini-2.5-pro", "google"),
        _model("google/gemini-2.5-flash", "google"),
    ]
    grouped = _group_hierarchy_v2(providers, models)
    assert [m["id"] for m in grouped[0]["models"]] == [
        "google/gemini-2.5-flash",
        "google/gemini-2.5-pro",
    ]


def test_each_provider_gets_exactly_one_entry_even_with_no_models():
    providers = [_provider("anthropic"), _provider("codex-cli")]
    grouped = _group_hierarchy_v2(providers, [])
    assert len(grouped) == 2
    assert grouped[0]["models"] == []
    assert grouped[1]["models"] == []


def test_models_orphaned_from_any_provider_are_dropped():
    """A model whose provider_id has no matching model_providers row
    doesn't render — the admin UI is provider-first, so it's better to
    hide the orphan than to surface a rootless card."""
    providers = [_provider("anthropic")]
    models = [
        _model("anthropic/claude-haiku-4-5", "anthropic"),
        _model("orphan/foo", "orphan"),
    ]
    grouped = _group_hierarchy_v2(providers, models)
    assert len(grouped) == 1
    assert [m["id"] for m in grouped[0]["models"]] == ["anthropic/claude-haiku-4-5"]


# ---------------------------------------------------------------------------
# Column projection — grouping preserves the fields the template consumes.
# ---------------------------------------------------------------------------


def test_provider_row_carries_all_required_fields():
    providers = [_provider("google",
                           base_url="https://x", api_key_source="Y", api_type="z",
                           runtime_status="not_provisioned", status="disabled")]
    grouped = _group_hierarchy_v2(providers, [])
    p = grouped[0]
    assert p["id"] == "google"
    assert p["label"] == "Google"
    assert p["base_url"] == "https://x"
    assert p["api_key_source"] == "Y"
    assert p["api_type"] == "z"
    assert p["runtime_status"] == "not_provisioned"
    assert p["status"] == "disabled"
    assert p["models"] == []


def test_model_row_carries_all_required_fields():
    providers = [_provider("google")]
    models = [_model("google/gemini-2.5-flash", "google",
                     tier="balanced", context_window=1_000_000,
                     provider_shorthand="flash",
                     available_context_windows=[100_000],
                     status="disabled")]
    grouped = _group_hierarchy_v2(providers, models)
    m = grouped[0]["models"][0]
    assert m["id"] == "google/gemini-2.5-flash"
    assert m["tier"] == "balanced"
    assert m["context_window"] == 1_000_000
    assert m["provider_shorthand"] == "flash"
    assert m["available_context_windows"] == [100_000]
    assert m["status"] == "disabled"


# ---------------------------------------------------------------------------
# synthesized_variants pre-expansion — the template must not need to
# import the synthesizer helper directly.
# ---------------------------------------------------------------------------


def test_synthesized_variants_populated_for_models_with_shorthand():
    providers = [_provider("google")]
    models = [_model(
        "google/gemini-2.5-flash", "google",
        provider_shorthand="flash",
        available_context_windows=[100_000, 200_000, 500_000],
    )]
    grouped = _group_hierarchy_v2(providers, models)
    sv = grouped[0]["models"][0]["synthesized_variants"]
    assert len(sv) == 3
    # Order preserved from the input array (which the backfill sorts asc).
    assert [v["context_window"] for v in sv] == [100_000, 200_000, 500_000]
    assert [v["variant_id"] for v in sv] == [
        "google-flash-100k/gemini-2.5-flash-100k",
        "google-flash-200k/gemini-2.5-flash-200k",
        "google-flash-500k/gemini-2.5-flash-500k",
    ]
    # Each variant carries the full 6-key dict from synthesize_variant.
    assert set(sv[0]) == {
        "variant_id", "provider_key", "upstream_id", "label", "context_window",
        "tags",
    }


def test_synthesized_variants_empty_for_native_only_model():
    providers = [_provider("claude-cli")]
    models = [_model("claude-cli/opus", "claude-cli",
                     provider_shorthand=None,
                     available_context_windows=[])]
    grouped = _group_hierarchy_v2(providers, models)
    assert grouped[0]["models"][0]["synthesized_variants"] == []


def test_synthesized_variants_empty_for_missing_available_context_windows_key():
    """Defensive: even if the DB read returned None for the array
    (shouldn't happen — DB default is '{}') the helper should still
    produce an empty list."""
    providers = [_provider("claude-cli")]
    models = [_model("claude-cli/opus", "claude-cli")]
    # Simulate a NULL: strip the key entirely from the model dict.
    del models[0]["available_context_windows"]
    grouped = _group_hierarchy_v2(providers, models)
    assert grouped[0]["models"][0]["synthesized_variants"] == []
    assert grouped[0]["models"][0]["available_context_windows"] == []


# ---------------------------------------------------------------------------
# effective_status — precedence-computed display field.
# ---------------------------------------------------------------------------


def test_effective_status_healthy_for_working_provider():
    providers = [_provider("anthropic", credential_status="healthy")]
    grouped = _group_hierarchy_v2(providers, [])
    assert grouped[0]["effective_status"] == "healthy"


def test_effective_status_disabled_wins_over_credential_state():
    providers = [_provider("anthropic", status="disabled", credential_status="revoked")]
    grouped = _group_hierarchy_v2(providers, [])
    assert grouped[0]["effective_status"] == "disabled"


def test_effective_status_not_provisioned_wins_over_everything():
    providers = [_provider(
        "anthropic", runtime_status="not_provisioned",
        status="disabled", credential_status="revoked",
    )]
    grouped = _group_hierarchy_v2(providers, [])
    assert grouped[0]["effective_status"] == "not_provisioned"


def test_effective_status_revoked_shown_for_active_provisioned_provider():
    providers = [_provider("anthropic", credential_status="revoked")]
    grouped = _group_hierarchy_v2(providers, [])
    assert grouped[0]["effective_status"] == "revoked"
