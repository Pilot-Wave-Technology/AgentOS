"""Unit test for `count_unpriced_active_models` (item C3 — pricing
coverage self-heal warning). No live Postgres; a minimal fake pool
exercises only `fetchval`, the sole call this function makes.
"""
from __future__ import annotations

import pytest

from app.services.model_pricing import count_unpriced_active_models


class _FakePool:
    def __init__(self, value):
        self._value = value
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self._value


@pytest.mark.asyncio
async def test_returns_zero_when_everything_is_priced():
    pool = _FakePool(0)
    result = await count_unpriced_active_models(pool)
    assert result == 0
    assert "v_model_pricing_coverage" in pool.calls[0][0]
    assert "NOT priced" in pool.calls[0][0]


@pytest.mark.asyncio
async def test_returns_nonzero_count_when_models_unpriced():
    pool = _FakePool(3)
    result = await count_unpriced_active_models(pool)
    assert result == 3
