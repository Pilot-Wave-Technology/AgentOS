"""The compaction summarizer reaches the gateway as an id its registry holds.

The gateway resolves `gateway_compaction` with `modelRegistry.find(provider,
id)`, and the catalog registers models only as context-window variants. A
base id (`google/gemini-3.8-flash`) is not found, and async compaction then
silently does nothing. The summarizer is served as the model's largest-window
variant.
"""
from __future__ import annotations

import os

import pytest

from app.services.model_registry.gateway_variant import largest_variant_id


def _row(model_id, shorthand, windows, label="M"):
    return {"id": model_id, "label": label, "provider_shorthand": shorthand,
            "available_context_windows": windows, "tags": []}


ROWS = [
    _row("google/gemini-3.8-flash", "flash38",
         [50_000, 100_000, 200_000, 500_000, 1_000_000], "Gemini 3.8 Flash"),
    _row("anthropic/native-only", "native", []),
]


def test_a_base_model_is_served_as_its_largest_variant():
    assert (largest_variant_id("google/gemini-3.8-flash", ROWS)
            == "google-flash38-1m/gemini-3.8-flash-1m")


def test_the_largest_window_wins_whatever_the_order():
    rows = [_row("google/gemini-3.8-flash", "flash38",
                 [1_000_000, 50_000, 500_000])]
    assert (largest_variant_id("google/gemini-3.8-flash", rows)
            == "google-flash38-1m/gemini-3.8-flash-1m")


def test_a_model_without_variants_is_served_unchanged():
    assert largest_variant_id("anthropic/native-only", ROWS) is None


def test_an_unknown_model_is_served_unchanged():
    assert largest_variant_id("google/not-registered", ROWS) is None


@pytest.mark.asyncio
async def test_the_default_summarizer_resolves_against_the_real_catalog():
    """The purpose's default maps to its 1M variant, and that variant is one
    the catalog actually registers with the gateway."""
    asyncpg = pytest.importorskip("asyncpg")
    dsn = os.environ.get("INTERLOCK_TEST_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("no database")
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, timeout=10)
    except Exception as exc:  # the repo's convention: no database, skip
        pytest.skip(f"database unreachable: {exc}")
    try:
        from app.services.misc_models import MISC_MODEL_PURPOSES
        from app.services.model_registry.gateway_variant import gateway_model_id
        from app.services.model_registry.queries import fetch_active_base_models

        default = MISC_MODEL_PURPOSES["gateway_compaction"]["default_model_id"]
        served = await gateway_model_id(pool, default)
        assert served != default, f"{default} has no registered variant"
        assert served.endswith("-1m"), served

        registered = set()
        from app.services.model_registry.synthesizer import synthesize_all_variants
        for row in await fetch_active_base_models(pool):
            registered.update(v["variant_id"] for v in synthesize_all_variants(row))
        assert served in registered
    finally:
        await pool.close()
