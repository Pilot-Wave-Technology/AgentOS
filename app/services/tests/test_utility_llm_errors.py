"""`generate_utility_text` must raise ONLY `UtilityLLMError`.

Regression guard for a bug found in review (2026-08-09): the function's
docstring promised it raises `UtilityLLMError` "on any failure ... or the
provider SDK call itself failing", but `_call_google`/`_call_anthropic`
had no try/except, so raw SDK exceptions escaped untouched.

That mattered because the hardcoded-Gemini cutover simultaneously
narrowed five call sites from `except Exception` to
`except UtilityLLMError` — `gemini_client.py`'s three entry points
(conversation summary, goal-summary reconcile, thread title),
`memory.py`'s compaction, and `resource_scanner.py`'s summarizer. Each
of those is written to degrade to a harmless fallback ("" / concatenated
titles) on failure; with the raw exception escaping, a transient provider
blip propagated into the caller's request instead of degrading.

These tests pin the exception CONTRACT rather than any one call site, so
the guarantee survives future call sites being added.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import utility_llm as u
from app.services.misc_models import SUPPORTED_UTILITY_PROVIDERS


def _patched(**overrides):
    """Patch out everything that would touch a DB, Secret Manager, or a
    provider SDK, leaving only the code path under test."""
    defaults = {
        "get_misc_model": AsyncMock(return_value="google/gemini-2.5-flash"),
        "_resolve_api_key": AsyncMock(return_value="fake-key"),
    }
    defaults.update(overrides)
    return [patch.object(u, name, value) for name, value in defaults.items()] + [
        patch.object(u, "get_pool", lambda: None)
    ]


async def _call_with(**overrides) -> Exception:
    """Run generate_utility_text under the given patches, returning the
    exception it raised (pytest.fail if it unexpectedly succeeded)."""
    patches = _patched(**overrides)
    for p in patches:
        p.start()
    try:
        try:
            await u.generate_utility_text("conversation_summary", "prompt")
        except Exception as exc:  # noqa: BLE001 — capturing the type IS the test
            return exc
        pytest.fail("expected an exception, call succeeded")
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_raw_google_sdk_error_becomes_utility_llm_error():
    exc = await _call_with(
        _call_google=AsyncMock(side_effect=RuntimeError("503 upstream unavailable")),
    )
    assert isinstance(exc, u.UtilityLLMError)
    # Original cause preserved for debugging, not swallowed.
    assert isinstance(exc.__cause__, RuntimeError)
    assert "503 upstream unavailable" in str(exc)


@pytest.mark.parametrize("provider", sorted(SUPPORTED_UTILITY_PROVIDERS))
@pytest.mark.asyncio
async def test_raw_sdk_error_becomes_utility_llm_error_for_every_supported_provider(provider):
    """Parametrized over the live `SUPPORTED_UTILITY_PROVIDERS` set rather
    than a hardcoded provider list, so this keeps covering whatever is
    actually enabled — including `anthropic` automatically, if/when it is
    re-enabled there (see that set's own note on why it is google-only
    today).

    Doubles as an invariant check: every supported provider must have a
    matching `_call_<provider>` adapter, so adding an id to the set
    without wiring an adapter fails here rather than at runtime.
    """
    adapter = f"_call_{provider}"
    assert hasattr(u, adapter), (
        f"{provider!r} is in SUPPORTED_UTILITY_PROVIDERS but utility_llm has "
        f"no {adapter}() adapter — it would raise AttributeError at runtime"
    )

    exc = await _call_with(
        get_misc_model=AsyncMock(return_value=f"{provider}/some-model"),
        **{adapter: AsyncMock(side_effect=RuntimeError("overloaded_error"))},
    )
    assert isinstance(exc, u.UtilityLLMError)
    assert "overloaded_error" in str(exc)


@pytest.mark.asyncio
async def test_credential_lookup_failure_becomes_utility_llm_error():
    """Secret Manager is a network call — it fails the same transient ways
    an SDK call does, so it needs the same translation."""
    exc = await _call_with(
        _resolve_api_key=AsyncMock(side_effect=OSError("secret manager unreachable")),
    )
    assert isinstance(exc, u.UtilityLLMError)
    assert "credential lookup failed" in str(exc)


@pytest.mark.asyncio
async def test_missing_credential_still_raises_utility_llm_error():
    exc = await _call_with(_resolve_api_key=AsyncMock(return_value=""))
    assert isinstance(exc, u.UtilityLLMError)
    assert "no credential available" in str(exc)


@pytest.mark.asyncio
async def test_unsupported_provider_raises_utility_llm_error():
    """A registry provider outside SUPPORTED_UTILITY_PROVIDERS — a CLI-only
    provider like `claude-cli`, or one whose SDK path exists but isn't
    enabled yet — must be a clean UtilityLLMError, not a KeyError or an
    attempt to call a nonexistent adapter.

    The provider id is derived rather than hardcoded so this stays valid
    no matter which providers are enabled.
    """
    unsupported = next(
        p for p in ("claude-cli", "openai", "codex-cli", "nonexistent-provider")
        if p not in SUPPORTED_UTILITY_PROVIDERS
    )
    exc = await _call_with(get_misc_model=AsyncMock(return_value=f"{unsupported}/some-model"))
    assert isinstance(exc, u.UtilityLLMError)
    assert "unsupported provider" in str(exc)


@pytest.mark.asyncio
async def test_malformed_wire_id_raises_utility_llm_error():
    exc = await _call_with(get_misc_model=AsyncMock(return_value="gemini-2.5-flash"))
    assert isinstance(exc, u.UtilityLLMError)
    assert "malformed wire id" in str(exc)
