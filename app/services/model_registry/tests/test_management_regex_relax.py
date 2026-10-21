"""Unit tests for the v2 ID-pattern relaxations in
`app/services/model_registry/management.py`.

Story 5dafee43 (cycle-01) first relaxed the character class on both
sides of `_MODEL_ID_RE` from lowercase-only to mixed-case + `._-`, and on
`_MODEL_PROVIDER_ID_RE` / `_PROVIDER_SHORTHAND_RE` too.

A second pass (see the docstring above `_MODEL_ID_RE`) went further on
the MODEL half specifically: it is no longer character-constrained at
all beyond excluding whitespace/control characters, and the
"exactly one slash" limit is gone — only the PROVIDER half still needs
a single unambiguous shape, because every downstream consumer (this
module's own `create_base_model_v2`, `synthesizer.derive_base_id`, and
the OpenClaw fork's `parseModelRef`/`splitModelRef`) splits on the FIRST
"/" only and keeps the remainder intact, so a multi-segment upstream
model id (e.g. Vertex's `publishers/google/models/gemini-pro`) is safe
to register as-is.

(The v1 legacy `_PROVIDER_NAME_RE` this file used to also pin was
removed 2026-08-06 along with the rest of the v1 CRUD surface — see
`management.py`'s module docstring.)

These tests pin the current, intentionally-asymmetric relaxation.
"""
from __future__ import annotations

import pytest

from app.services.model_registry import management as m


# ===========================================================================
# _MODEL_ID_RE — provider half constrained (mixed-case + `._-`, no
# slash); model half free text (no whitespace/control chars), including
# further slashes.
# ===========================================================================


@pytest.mark.parametrize(
    "good_id",
    [
        # Real-world upstream conventions the original relaxation called out.
        "Anthropic/Claude-Opus",
        "openai/o3-mini",
        "Google/gemini-2.5-flash.preview.04-17",
        "MyOrg/my_model",
        # Lower-case still passes (backward-compat).
        "anthropic/claude-sonnet-4-5",
        "google/gemini-2.5-flash",
        # Digits + dots + hyphens on both sides.
        "provider.v2/model.v1-final",
        # Multi-segment model half — the actual point of the second
        # relaxation pass. Provider stays unambiguous; everything after
        # the first slash is free text.
        "vertex/publishers/google/models/gemini-pro",
        "a/b/c",
        # Model half may start with a character the provider half
        # would reject, and may contain symbols the provider half
        # would reject too — it's no longer character-constrained.
        "foo/-bar",
        "foo/.bar",
        "foo/bar@baz",
        "foo/bar:baz",
    ],
)
def test_model_id_relaxed_accepts_previously_rejected_shapes(good_id):
    assert m._MODEL_ID_RE.fullmatch(good_id) is not None, good_id


@pytest.mark.parametrize(
    "bad_id",
    [
        # No slash at all — there is nothing to split into provider/model.
        "foo",
        "no-slash",
        # Empty provider half.
        "/x",
        # Empty model half.
        "x/",
        "/",
        # Empty entirely.
        "",
        # Provider start-char rule: must be a letter or digit, not
        # `.`, `_`, `-`. This is the one shape rule that remains,
        # because the provider half is what every downstream consumer
        # actually depends on being unambiguous.
        "-foo/bar",
        ".foo/bar",
        "_foo/bar",
        # Space in the provider half — still rejected (provider is the
        # constrained side).
        "foo bar/baz",
        # Space anywhere in the model half is still rejected too — not
        # because anything downstream is known to break on it, but
        # there's no real upstream model id that needs whitespace, and
        # keeping it out avoids relying on template auto-escaping alone.
        "foo/bar baz",
    ],
)
def test_model_id_structural_rule_still_enforced(bad_id):
    assert m._MODEL_ID_RE.fullmatch(bad_id) is None, bad_id


def test_validate_model_id_accepts_multi_slash_model_half():
    """The public validator must NOT raise on a multi-segment model
    half — this is the behavior the second relaxation pass exists for."""
    m._validate_model_id("vertex/publishers/google/models/gemini-pro")  # must not raise


def test_validate_model_id_still_raises_on_bad_provider_half():
    with pytest.raises(m.ModelValidationError) as ei:
        m._validate_model_id("-badprovider/bar")
    assert ei.value.error_code == "invalid_id"


def test_validate_model_id_accepts_uppercase_shapes():
    """Sanity: the previously-rejected mixed-case id passes cleanly."""
    m._validate_model_id("Anthropic/Claude-Opus")  # must not raise


# ===========================================================================
# _MODEL_PROVIDER_ID_RE — mixed-case + `._-`, no slash allowed.
# ===========================================================================


@pytest.mark.parametrize(
    "good_pid",
    [
        # Uppercase / mixed case.
        "Anthropic",
        "Google",
        "MyOrg",
        # Lowercase + hyphen (pre-relaxation shape still passes).
        "openai",
        "codex-cli",
        "claude-cli",
        # Dot + underscore.
        "azure.us",
        "my_org",
        # Digits + hyphens.
        "provider-42",
    ],
)
def test_model_provider_id_relaxed_accepts_new_shapes(good_pid):
    assert m._MODEL_PROVIDER_ID_RE.fullmatch(good_pid) is not None, good_pid


@pytest.mark.parametrize(
    "bad_pid",
    [
        # Slash is the model-id separator — reject in a provider-only field.
        "foo/bar",
        # Empty.
        "",
        # Start-char rule.
        "-badstart",
        ".dotstart",
        "_undestart",
        # Non-alphabet character.
        "bad space",
        "bad@char",
        "bad:char",
    ],
)
def test_model_provider_id_rejects_invalid_shapes(bad_pid):
    assert m._MODEL_PROVIDER_ID_RE.fullmatch(bad_pid) is None, bad_pid


def test_validate_model_provider_id_raises_on_slash():
    with pytest.raises(m.ProviderValidationError) as ei:
        m._validate_model_provider_id("foo/bar")
    assert ei.value.error_code == "invalid_id"


def test_validate_model_provider_id_accepts_uppercase():
    m._validate_model_provider_id("Anthropic")  # must not raise


# ===========================================================================
# _PROVIDER_SHORTHAND_RE — same alphabet as v2 provider id.
# ===========================================================================


@pytest.mark.parametrize(
    "good_shorthand",
    [
        "flash", "pro", "sonnet", "sonnet5", "opus", "opus48",
        # Uppercase now accepted.
        "Flash", "Sonnet5",
        # Dot / underscore.
        "preview.04-17", "my_variant",
    ],
)
def test_provider_shorthand_relaxed_accepts_new_shapes(good_shorthand):
    assert m._PROVIDER_SHORTHAND_RE.fullmatch(good_shorthand) is not None, good_shorthand


@pytest.mark.parametrize(
    "bad_shorthand",
    [
        "", "-nope", ".nope", "_nope",
        "bad space", "bad/slash", "bad@char",
    ],
)
def test_provider_shorthand_rejects_invalid_shapes(bad_shorthand):
    assert m._PROVIDER_SHORTHAND_RE.fullmatch(bad_shorthand) is None, bad_shorthand


# ===========================================================================
# _PROVIDER_NAME_RE — v1 legacy wire surface, DELIBERATELY UNCHANGED.
# ===========================================================================


# NOTE: this section used to pin `_PROVIDER_NAME_RE`/`_validate_provider_name`
# (the legacy v1 `model_provider_registry.name` wire-format regex,
# deliberately NOT relaxed in parallel with the v2 provider-id regex
# above). Removed 2026-08-06 along with the rest of the v1 CRUD surface
# (zero live callers, confirmed by repo-wide grep) — there is no longer
# a v1 wire surface for this regex to protect.


# ===========================================================================
# Cross-check: relaxation did not accidentally erase the character
# alphabet difference between the model-id sides and the shorthand.
# ===========================================================================


def test_relaxed_regexes_do_not_admit_slash_in_provider_scoped_fields():
    """Provider id + shorthand must never accept a slash (the model-id
    separator). Regressions on this would silently corrupt the
    `<provider>/<model>` shape the fork depends on."""
    assert m._MODEL_PROVIDER_ID_RE.fullmatch("has/slash") is None
    assert m._PROVIDER_SHORTHAND_RE.fullmatch("has/slash") is None


def test_model_id_no_longer_requires_exactly_one_slash():
    """SUPERSEDES the old "load-bearing invariant" claim this test used
    to pin. Verified against the actual downstream consumers (this
    module's own first-slash split, `synthesizer.derive_base_id`, and
    the OpenClaw fork's `parseModelRef`/`splitModelRef`, the latter
    fixed to match) that only the PROVIDER half needs to be
    unambiguous — a second or third slash inside the model half is
    safe because everything after the first slash is always kept
    intact, never re-split."""
    assert m._MODEL_ID_RE.fullmatch("a/b/c") is not None
    assert m._MODEL_ID_RE.fullmatch("a/b") is not None
