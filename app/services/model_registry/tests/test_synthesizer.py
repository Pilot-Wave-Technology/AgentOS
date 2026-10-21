"""Unit tests for `app/services/model_registry/synthesizer.py`.

The helper is pure (no I/O, no DB), so these tests are entirely
in-process. The primary fixture mirrors every synthetic provider entry
that ships in `/opt/agent_os_openclaw/infra/config/models-synthetic.json`
today — 25 entries across 6 base models. Each row is a full snapshot
(variant_id + provider_key + upstream_id + label + context_window),
so any drift between the helper and the gateway config surfaces here
as a per-row assert failure rather than a silent divergence.

Cycle-01 of story 414f92d6.
"""
from __future__ import annotations

import pytest

from app.services.model_registry.synthesizer import (
    _to_size_suffix,
    derive_base_id,
    synthesize_all_variants,
    synthesize_variant,
)


# ---------------------------------------------------------------------------
# 25-row snapshot of models-synthetic.json.
#
# Columns: (provider_key, base_id, base_label, shorthand, context_tokens,
#           expected variant_id, expected label).
# `upstream_id` is always the base_id — the helper does not recompute it.
# ---------------------------------------------------------------------------


_SYNTH_SNAPSHOT: list[tuple] = [
    # google-flash-* wraps google/gemini-2.5-flash
    ("google-flash-50k",   "google/gemini-2.5-flash",     "Gemini 2.5 Flash",     "flash",   50_000,
     "google-flash-50k/gemini-2.5-flash-50k",             "Gemini 2.5 Flash (50K test)"),
    ("google-flash-100k",  "google/gemini-2.5-flash",     "Gemini 2.5 Flash",     "flash",  100_000,
     "google-flash-100k/gemini-2.5-flash-100k",           "Gemini 2.5 Flash (100K test)"),
    ("google-flash-200k",  "google/gemini-2.5-flash",     "Gemini 2.5 Flash",     "flash",  200_000,
     "google-flash-200k/gemini-2.5-flash-200k",           "Gemini 2.5 Flash (200K test)"),
    ("google-flash-500k",  "google/gemini-2.5-flash",     "Gemini 2.5 Flash",     "flash",  500_000,
     "google-flash-500k/gemini-2.5-flash-500k",           "Gemini 2.5 Flash (500K test)"),
    ("google-flash-1m",    "google/gemini-2.5-flash",     "Gemini 2.5 Flash",     "flash",  1_000_000,
     "google-flash-1m/gemini-2.5-flash-1m",               "Gemini 2.5 Flash (1M test)"),

    # google-pro-* wraps google/gemini-2.5-pro
    ("google-pro-100k",    "google/gemini-2.5-pro",       "Gemini 2.5 Pro",       "pro",    100_000,
     "google-pro-100k/gemini-2.5-pro-100k",               "Gemini 2.5 Pro (100K test)"),
    ("google-pro-200k",    "google/gemini-2.5-pro",       "Gemini 2.5 Pro",       "pro",    200_000,
     "google-pro-200k/gemini-2.5-pro-200k",               "Gemini 2.5 Pro (200K test)"),
    ("google-pro-500k",    "google/gemini-2.5-pro",       "Gemini 2.5 Pro",       "pro",    500_000,
     "google-pro-500k/gemini-2.5-pro-500k",               "Gemini 2.5 Pro (500K test)"),
    ("google-pro-1m",      "google/gemini-2.5-pro",       "Gemini 2.5 Pro",       "pro",    1_000_000,
     "google-pro-1m/gemini-2.5-pro-1m",                   "Gemini 2.5 Pro (1M test)"),

    # anthropic-sonnet-* wraps anthropic/claude-sonnet-4-5
    ("anthropic-sonnet-50k",  "anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet",  50_000,
     "anthropic-sonnet-50k/claude-sonnet-4-5-50k",        "Claude Sonnet 4.5 (50K test)"),
    ("anthropic-sonnet-100k", "anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet", 100_000,
     "anthropic-sonnet-100k/claude-sonnet-4-5-100k",      "Claude Sonnet 4.5 (100K test)"),
    ("anthropic-sonnet-200k", "anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet", 200_000,
     "anthropic-sonnet-200k/claude-sonnet-4-5-200k",      "Claude Sonnet 4.5 (200K test)"),
    ("anthropic-sonnet-500k", "anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet", 500_000,
     "anthropic-sonnet-500k/claude-sonnet-4-5-500k",      "Claude Sonnet 4.5 (500K test)"),
    ("anthropic-sonnet-1m",   "anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet", 1_000_000,
     "anthropic-sonnet-1m/claude-sonnet-4-5-1m",          "Claude Sonnet 4.5 (1M test)"),

    # anthropic-opus-* wraps anthropic/claude-opus-4-6
    ("anthropic-opus-100k", "anthropic/claude-opus-4-6", "Claude Opus 4.6", "opus",   100_000,
     "anthropic-opus-100k/claude-opus-4-6-100k",          "Claude Opus 4.6 (100K test)"),
    ("anthropic-opus-200k", "anthropic/claude-opus-4-6", "Claude Opus 4.6", "opus",   200_000,
     "anthropic-opus-200k/claude-opus-4-6-200k",          "Claude Opus 4.6 (200K test)"),
    ("anthropic-opus-500k", "anthropic/claude-opus-4-6", "Claude Opus 4.6", "opus",   500_000,
     "anthropic-opus-500k/claude-opus-4-6-500k",          "Claude Opus 4.6 (500K test)"),
    ("anthropic-opus-1m",   "anthropic/claude-opus-4-6", "Claude Opus 4.6", "opus",   1_000_000,
     "anthropic-opus-1m/claude-opus-4-6-1m",              "Claude Opus 4.6 (1M test)"),

    # anthropic-opus48-* wraps anthropic/claude-opus-4-8 (digit inside shorthand)
    ("anthropic-opus48-100k", "anthropic/claude-opus-4-8", "Claude Opus 4.8", "opus48", 100_000,
     "anthropic-opus48-100k/claude-opus-4-8-100k",        "Claude Opus 4.8 (100K test)"),
    ("anthropic-opus48-200k", "anthropic/claude-opus-4-8", "Claude Opus 4.8", "opus48", 200_000,
     "anthropic-opus48-200k/claude-opus-4-8-200k",        "Claude Opus 4.8 (200K test)"),
    ("anthropic-opus48-500k", "anthropic/claude-opus-4-8", "Claude Opus 4.8", "opus48", 500_000,
     "anthropic-opus48-500k/claude-opus-4-8-500k",        "Claude Opus 4.8 (500K test)"),

    # anthropic-sonnet5-* wraps anthropic/claude-sonnet-5
    ("anthropic-sonnet5-50k",  "anthropic/claude-sonnet-5", "Claude Sonnet 5", "sonnet5",  50_000,
     "anthropic-sonnet5-50k/claude-sonnet-5-50k",         "Claude Sonnet 5 (50K test)"),
    ("anthropic-sonnet5-100k", "anthropic/claude-sonnet-5", "Claude Sonnet 5", "sonnet5", 100_000,
     "anthropic-sonnet5-100k/claude-sonnet-5-100k",       "Claude Sonnet 5 (100K test)"),
    ("anthropic-sonnet5-200k", "anthropic/claude-sonnet-5", "Claude Sonnet 5", "sonnet5", 200_000,
     "anthropic-sonnet5-200k/claude-sonnet-5-200k",       "Claude Sonnet 5 (200K test)"),
    ("anthropic-sonnet5-500k", "anthropic/claude-sonnet-5", "Claude Sonnet 5", "sonnet5", 500_000,
     "anthropic-sonnet5-500k/claude-sonnet-5-500k",       "Claude Sonnet 5 (500K test)"),
]


def test_snapshot_size_matches_gateway_config():
    """25 synthetic entries as of writing — flag on drift."""
    assert len(_SYNTH_SNAPSHOT) == 25


@pytest.mark.parametrize(
    "provider_key,base_id,base_label,shorthand,tokens,expected_variant_id,expected_label",
    _SYNTH_SNAPSHOT,
)
def test_synthesize_variant_matches_gateway_snapshot(
    provider_key, base_id, base_label, shorthand, tokens,
    expected_variant_id, expected_label,
):
    """Every models-synthetic.json entry is reproducible from a helper call."""
    out = synthesize_variant(
        base_id=base_id,
        base_label=base_label,
        provider_shorthand=shorthand,
        context_tokens=tokens,
    )
    assert out == {
        "variant_id": expected_variant_id,
        "provider_key": provider_key,
        "upstream_id": base_id,
        "label": expected_label,
        "context_window": tokens,
        "tags": [],
    }


# ---------------------------------------------------------------------------
# _to_size_suffix — happy paths + ValueError branches.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tokens,wire,display",
    [
        (50_000,    "50k", "50K"),
        (100_000,   "100k", "100K"),
        (200_000,   "200k", "200K"),
        (500_000,   "500k", "500K"),
        (1_000_000, "1m",   "1M"),
        # Not in seed but exercise both conversion branches.
        (2_000_000, "2m",   "2M"),
        (10_000_000, "10m", "10M"),
        (1_000,     "1k",   "1K"),
    ],
)
def test_to_size_suffix_happy(tokens, wire, display):
    assert _to_size_suffix(tokens) == (wire, display)


@pytest.mark.parametrize("bad", [0, -1, -100, 999, 1500, 1_000_001])
def test_to_size_suffix_rejects_non_power_of_1000(bad):
    with pytest.raises(ValueError):
        _to_size_suffix(bad)


@pytest.mark.parametrize("bad", ["100k", 1.5, None, True, False])
def test_to_size_suffix_rejects_non_int(bad):
    # bool subclasses int in Python; the helper rejects it explicitly.
    with pytest.raises(ValueError):
        _to_size_suffix(bad)


def test_to_size_suffix_prefers_m_over_k_at_whole_million():
    # 1_000_000 is a multiple of both 1000 (→ '1000k') and 1_000_000 (→ '1m');
    # the helper's contract is to prefer the M form.
    assert _to_size_suffix(1_000_000) == ("1m", "1M")
    assert _to_size_suffix(3_000_000) == ("3m", "3M")


# ---------------------------------------------------------------------------
# synthesize_variant — ValueError branches beyond size-suffix failure.
# ---------------------------------------------------------------------------


def test_synthesize_variant_rejects_empty_shorthand():
    with pytest.raises(ValueError):
        synthesize_variant("google/gemini-2.5-flash", "Gemini 2.5 Flash", "", 100_000)


def test_synthesize_variant_rejects_none_shorthand():
    with pytest.raises(ValueError):
        synthesize_variant("google/gemini-2.5-flash", "Gemini 2.5 Flash", None, 100_000)


def test_synthesize_variant_rejects_base_id_without_slash():
    with pytest.raises(ValueError):
        synthesize_variant("googleflash", "X", "flash", 100_000)


def test_synthesize_variant_propagates_size_suffix_errors():
    with pytest.raises(ValueError):
        synthesize_variant("google/gemini-2.5-flash", "Gemini 2.5 Flash", "flash", 999)


# ---------------------------------------------------------------------------
# synthesize_all_variants — empty vs populated.
# ---------------------------------------------------------------------------


def test_synthesize_all_variants_empty_windows_returns_empty_list():
    row = {
        "id": "claude-cli/opus",
        "label": "Claude CLI Opus",
        "provider_shorthand": None,
        "available_context_windows": [],
    }
    assert synthesize_all_variants(row) == []


def test_synthesize_all_variants_missing_key_returns_empty_list():
    row = {
        "id": "claude-cli/opus",
        "label": "Claude CLI Opus",
        "provider_shorthand": None,
        # available_context_windows absent
    }
    assert synthesize_all_variants(row) == []


def test_synthesize_all_variants_returns_one_dict_per_window_in_input_order():
    row = {
        "id": "google/gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "provider_shorthand": "flash",
        "available_context_windows": [50_000, 100_000, 200_000, 500_000, 1_000_000],
    }
    outs = synthesize_all_variants(row)
    assert len(outs) == 5
    assert [o["context_window"] for o in outs] == [50_000, 100_000, 200_000, 500_000, 1_000_000]
    assert [o["variant_id"] for o in outs] == [
        "google-flash-50k/gemini-2.5-flash-50k",
        "google-flash-100k/gemini-2.5-flash-100k",
        "google-flash-200k/gemini-2.5-flash-200k",
        "google-flash-500k/gemini-2.5-flash-500k",
        "google-flash-1m/gemini-2.5-flash-1m",
    ]


# ---------------------------------------------------------------------------
# derive_base_id — the reverse of synthesize_variant.
# ---------------------------------------------------------------------------


def test_derive_base_id_simple_native_name():
    assert derive_base_id(
        "google-flash-100k/gemini-2.5-flash-100k"
    ) == "google/gemini-2.5-flash"


def test_derive_base_id_multi_hyphen_native_name():
    """A multi-hyphen native name (e.g. 'claude-opus-4-8') must round-trip
    — the derivation strips only the trailing size suffix, not a fixed
    number of hyphen segments."""
    assert derive_base_id(
        "anthropic-opus48-500k/claude-opus-4-8-500k"
    ) == "anthropic/claude-opus-4-8"


def test_derive_base_id_million_suffix():
    assert derive_base_id(
        "google-flash-1m/gemini-2.5-flash-1m"
    ) == "google/gemini-2.5-flash"


def test_derive_base_id_round_trips_every_synthesize_variant_output():
    """For every (base, size) pair `synthesize_variant` can produce,
    `derive_base_id` must recover the exact original base_id."""
    cases = [
        ("google/gemini-2.5-flash", "flash", 100_000),
        ("anthropic/claude-sonnet-4-5", "sonnet", 50_000),
        ("anthropic/claude-opus-4-8", "opus48", 1_000_000),
        ("anthropic/claude-sonnet-5", "sonnet5", 200_000),
    ]
    for base_id, shorthand, tokens in cases:
        base_label = base_id.split("/", 1)[1]
        variant = synthesize_variant(base_id, base_label, shorthand, tokens)
        assert derive_base_id(variant["variant_id"]) == base_id


def test_derive_base_id_plain_base_id_returns_none():
    """A plain base id (no size suffix at all) isn't a variant —
    'google/gemini-2.5-flash' has no '-' in its provider prefix."""
    assert derive_base_id("google/gemini-2.5-flash") is None


def test_derive_base_id_unsized_native_returns_none():
    """An unsized native/CLI id with a hyphen in its provider prefix but
    no matching size suffix on the model name isn't a variant either."""
    assert derive_base_id("claude-cli/opus") is None


def test_derive_base_id_no_slash_returns_none():
    assert derive_base_id("not-a-wire-id") is None


# ---------------------------------------------------------------------------
# tags propagation — a variant has the SAME capabilities as its base.
# ---------------------------------------------------------------------------


def test_synthesize_variant_passes_through_tags():
    out = synthesize_variant(
        "anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet",
        100_000, tags=["reasoning", "vision"],
    )
    assert out["tags"] == ["reasoning", "vision"]


def test_synthesize_variant_defaults_tags_to_empty_list():
    out = synthesize_variant(
        "google/gemini-2.5-flash", "Gemini 2.5 Flash", "flash", 100_000,
    )
    assert out["tags"] == []


def test_synthesize_all_variants_propagates_base_tags_to_every_variant():
    row = {
        "id": "anthropic/claude-sonnet-4-5",
        "label": "Claude Sonnet 4.5",
        "provider_shorthand": "sonnet",
        "available_context_windows": [100_000, 200_000],
        "tags": ["reasoning", "vision"],
    }
    outs = synthesize_all_variants(row)
    assert len(outs) == 2
    for out in outs:
        assert out["tags"] == ["reasoning", "vision"]


def test_synthesize_all_variants_missing_tags_key_defaults_empty():
    """A model_row without a 'tags' key at all (e.g. an older test
    fixture that predates this field) still works — empty tags, not a
    KeyError."""
    row = {
        "id": "google/gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "provider_shorthand": "flash",
        "available_context_windows": [100_000],
    }
    outs = synthesize_all_variants(row)
    assert outs[0]["tags"] == []
