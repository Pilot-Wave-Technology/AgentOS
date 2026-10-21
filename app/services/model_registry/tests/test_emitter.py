"""Unit tests for the wire-contract emitter.

Locks the emitter to the hardcoded {id, name, contextWindow, reasoning,
input} / {upstream, upstreamId, contextWindow} whitelists so a future
`dict(row)` refactor cannot silently leak columns. Both builders take
active BASE `models` rows and synthesize context-window variants on the
fly — there is no separate variant row to read (see
`management.add_context_window_v2`'s docstring).
"""
import json

from app.services.model_registry.emitter import (
    build_models_payload,
    build_provider_credentials_payload,
    build_providers_payload,
)

_MODEL_ENTRY_KEYS = {"id", "name", "contextWindow", "reasoning", "input"}


def _base_row(**over) -> dict:
    row = {
        "id": "google/gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "context_window": 1_000_000,
        "provider_shorthand": None,
        "available_context_windows": [],
        "tags": [],
    }
    row.update(over)
    return row


def test_build_models_payload_base_only_no_variants():
    """A base row with no context windows emits only its own entry.
    Untagged: reasoning=False, input=['text'] — the same default the
    gateway itself applies when a field is absent (see
    `emitter._DEFAULT_INPUT`'s docstring)."""
    rows = [_base_row()]
    payload = build_models_payload(rows)

    assert set(payload.keys()) == {"google/gemini-2.5-flash"}
    assert payload["google/gemini-2.5-flash"] == {
        "id": "google/gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "contextWindow": 1_000_000,
        "reasoning": False,
        "input": ["text"],
    }


def test_build_models_payload_capability_fields_from_tags():
    """'reasoning' tag -> reasoning=True; 'vision' tag -> "image" added
    to input."""
    rows = [_base_row(tags=["reasoning", "vision"])]
    payload = build_models_payload(rows)
    entry = payload["google/gemini-2.5-flash"]
    assert entry["reasoning"] is True
    assert entry["input"] == ["text", "image"]


def test_build_models_payload_includes_synthesized_variants():
    """A base row with context windows emits itself PLUS one entry per
    synthesized variant — exactly the 5-key shape each, and each variant
    inherits the base's capability fields (same model, different size)."""
    rows = [_base_row(provider_shorthand="flash",
                       available_context_windows=[100_000, 200_000],
                       tags=["vision"])]
    payload = build_models_payload(rows)

    assert set(payload.keys()) == {
        "google/gemini-2.5-flash",
        "google-flash-100k/gemini-2.5-flash-100k",
        "google-flash-200k/gemini-2.5-flash-200k",
    }
    for key, entry in payload.items():
        assert set(entry.keys()) == _MODEL_ENTRY_KEYS, (
            f"unexpected fields for {key}: {sorted(entry.keys())}"
        )
    assert payload["google-flash-100k/gemini-2.5-flash-100k"] == {
        "id": "google-flash-100k/gemini-2.5-flash-100k",
        "name": "Gemini 2.5 Flash (100K test)",
        "contextWindow": 100_000,
        "reasoning": False,
        "input": ["text", "image"],
    }


def test_build_models_payload_no_column_leakage():
    """A future dict(row) refactor is caught by this guard.

    Any column present on the row that is NOT one of id/label/
    context_window must NOT appear anywhere in the emitted JSON.
    """
    rows = [
        _base_row(
            # Poison columns — a future accidental dict(row) splat
            # would surface these into the wire payload.
            internal_secret="leak_me",
            provider_id="google",
            tier="fast",
            status="active",
        ),
    ]

    payload = build_models_payload(rows)

    serialised = json.dumps(payload)
    for poison in (
        "internal_secret",
        "leak_me",
        "provider_id",
        '"tier"',        # match the JSON key, not the "test" value strings elsewhere
        '"status"',
        "active",
    ):
        assert poison not in serialised, (
            f"poison value/key {poison!r} leaked into payload: {serialised}"
        )


def test_build_models_payload_empty():
    assert build_models_payload([]) == {}


def _native(id="google", base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_source="GEMINI_API_KEY", api_type="google-generative-ai") -> dict:
    return {
        "id": id, "base_url": base_url,
        "api_key_source": api_key_source, "api_type": api_type,
    }


_NATIVE_ROWS = [
    _native(),
    _native(id="anthropic", base_url="https://api.anthropic.com/v1",
            api_key_source="ANTHROPIC_API_KEY", api_type="anthropic-messages"),
]


def test_build_providers_payload_full_provider_config_shape():
    """Provider payload nests a full model list under each synthetic
    provider, with baseUrl/apiKey/api looked up from the native provider
    row sharing its upstream — the real `ProviderConfig` shape the
    gateway's `resolve_providers` merge pipeline requires (§7a), not the
    earlier {upstream, upstreamId, contextWindow} shape."""
    rows = [
        _base_row(id="google/gemini-2.5-flash", label="Gemini 2.5 Flash",
                  provider_shorthand="flash",
                  available_context_windows=[100_000], tags=["vision"]),
        _base_row(id="anthropic/claude-sonnet-4-5", label="Claude Sonnet 4.5",
                  provider_shorthand="sonnet",
                  available_context_windows=[1_000_000], tags=["reasoning"]),
    ]

    payload = build_providers_payload(rows, _NATIVE_ROWS)

    assert payload == {
        "google-flash-100k": {
            "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
            "apiKey": "GEMINI_API_KEY",
            "api": "google-generative-ai",
            "models": [{
                "id": "gemini-2.5-flash-100k",
                "name": "Gemini 2.5 Flash (100K test)",
                "upstreamId": "gemini-2.5-flash",
                "contextWindow": 100_000,
                "reasoning": False,
                "input": ["text", "image"],
            }],
        },
        "anthropic-sonnet-1m": {
            "baseUrl": "https://api.anthropic.com/v1",
            "apiKey": "ANTHROPIC_API_KEY",
            "api": "anthropic-messages",
            "models": [{
                "id": "claude-sonnet-4-5-1m",
                "name": "Claude Sonnet 4.5 (1M test)",
                "upstreamId": "claude-sonnet-4-5",
                "contextWindow": 1_000_000,
                "reasoning": True,
                "input": ["text"],
            }],
        },
        # Native (bare-id) entries — one per base row, at its own native
        # context_window, so the "select the model at its real native
        # window" GO option (dropdowns.build_go_model_tree_v2) resolves
        # to an actual gateway provider entry instead of silently falling
        # back (see build_providers_payload's docstring).
        "google": {
            "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
            "apiKey": "GEMINI_API_KEY",
            "api": "google-generative-ai",
            "models": [{
                "id": "gemini-2.5-flash",
                "name": "Gemini 2.5 Flash",
                "upstreamId": "gemini-2.5-flash",
                "contextWindow": 1_000_000,
                "reasoning": False,
                "input": ["text", "image"],
            }],
        },
        "anthropic": {
            "baseUrl": "https://api.anthropic.com/v1",
            "apiKey": "ANTHROPIC_API_KEY",
            "api": "anthropic-messages",
            "models": [{
                "id": "claude-sonnet-4-5",
                "name": "Claude Sonnet 4.5",
                "upstreamId": "claude-sonnet-4-5",
                "contextWindow": 1_000_000,
                "reasoning": True,
                "input": ["text"],
            }],
        },
    }


def test_build_providers_payload_multi_hyphen_native_name():
    """A multi-hyphen native model name (e.g. 'claude-opus-4-8') must
    round-trip correctly — the derivation strips only the trailing size
    suffix, not a fixed number of hyphen segments."""
    rows = [
        _base_row(id="anthropic/claude-opus-4-8", label="Claude Opus 4.8",
                  provider_shorthand="opus48",
                  available_context_windows=[500_000]),
    ]
    payload = build_providers_payload(rows, _NATIVE_ROWS)
    assert payload["anthropic-opus48-500k"]["models"][0]["upstreamId"] == "claude-opus-4-8"
    assert payload["anthropic-opus48-500k"]["api"] == "anthropic-messages"


def test_build_providers_payload_skips_variant_with_no_matching_native_provider():
    """A variant whose upstream has no native provider row (e.g. removed
    or missing base_url/api_key_source) is skipped entirely — a partial
    entry with no connection details would be worse than none."""
    rows = [
        _base_row(id="openai/gpt-6", label="GPT 6",
                  provider_shorthand="gpt6",
                  available_context_windows=[100_000]),
    ]
    assert build_providers_payload(rows, native_providers=[]) == {}


def test_build_providers_payload_multiple_variants_share_one_provider_entry():
    """Two context windows on the same base still only produce entries
    under their own distinct provider_key each (one variant per synthetic
    provider, by construction — see synthesizer.py), plus the base's own
    native (bare-id) entry."""
    rows = [
        _base_row(id="google/gemini-2.5-flash", label="Gemini 2.5 Flash",
                  provider_shorthand="flash",
                  available_context_windows=[100_000, 200_000]),
    ]
    payload = build_providers_payload(rows, _NATIVE_ROWS)
    assert set(payload.keys()) == {"google-flash-100k", "google-flash-200k", "google"}
    assert len(payload["google-flash-100k"]["models"]) == 1


def test_build_providers_payload_no_synthesized_variants_still_emits_native_entry():
    """A base row with no context windows contributes no SYNTHESIZED
    variant entries, but still gets its own native (bare-id) entry — the
    "use the model at its real native window" GO option needs an actual
    gateway provider entry to resolve against, same as any variant."""
    rows = [_base_row()]
    payload = build_providers_payload(rows, _NATIVE_ROWS)
    assert set(payload.keys()) == {"google"}
    assert payload["google"]["models"] == [{
        "id": "gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "upstreamId": "gemini-2.5-flash",
        "contextWindow": 1_000_000,
        "reasoning": False,
        "input": ["text"],
    }]


def test_build_providers_payload_empty():
    assert build_providers_payload([], []) == {}


# ===========================================================================
# build_provider_credentials_payload
# ===========================================================================


def _native_provider_row(**over) -> dict:
    row = {
        "id": "anthropic",
        "secret_project": "1097695349140",
        "secret_id": "claude-api-key-feature",
        "secret_version": "latest",
        "credential_status": "healthy",
    }
    row.update(over)
    return row


def test_build_provider_credentials_payload_includes_healthy_provider():
    out = build_provider_credentials_payload([_native_provider_row()])
    assert out == {
        "anthropic": {
            "secretProject": "1097695349140",
            "secretId": "claude-api-key-feature",
            "secretVersion": "latest",
        },
    }


def test_build_provider_credentials_payload_excludes_provider_with_no_secret_id():
    out = build_provider_credentials_payload([_native_provider_row(secret_id=None)])
    assert out == {}


def test_build_provider_credentials_payload_excludes_revoked_provider():
    """The mechanism that actually makes 'Revoke' stop the gateway from
    using the credential — see revoke_provider_credential_v2's
    docstring in management.py. Secret Manager itself is untouched;
    this just stops handing the gateway a reference to resolve."""
    out = build_provider_credentials_payload([_native_provider_row(credential_status="revoked")])
    assert out == {}


def test_build_provider_credentials_payload_includes_unhealthy_and_unknown():
    """Only 'revoked' is excluded — 'unhealthy'/'unknown' still get
    handed to the gateway so it keeps retrying resolution (that's how
    a transient failure recovers on its own)."""
    out = build_provider_credentials_payload([
        _native_provider_row(id="anthropic", credential_status="unhealthy"),
        _native_provider_row(id="google", secret_id="gemini-api-key-feature", credential_status="unknown"),
    ])
    assert set(out.keys()) == {"anthropic", "google"}
