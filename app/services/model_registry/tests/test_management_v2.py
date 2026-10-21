"""Unit tests for the v2 surface of `app/services/model_registry/management.py`.

Cycle-01 of story 414f92d6. Drives every `*_v2` function + the two
context-window helpers through the same offline FakePool / FakeConn
recorder pattern the pre-existing test_management.py uses — no live
Postgres. Covers happy paths + every named validation branch listed
in the task description.

The FakeConn helpers below are a self-contained copy of the ones in
test_management.py (a small deliberate duplication so this file's
tests are independent of that file's evolution).
"""
from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from app.services.model_registry import management as m


# ---------------------------------------------------------------------------
# Test doubles for asyncpg (see test_management.py for the pattern).
# ---------------------------------------------------------------------------


class _CannedResponse:
    def __init__(self, exc: BaseException):
        self.exc = exc


def raises(exc: BaseException) -> _CannedResponse:
    return _CannedResponse(exc)


class FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.responses: dict[str, list[Any]] = {
            "fetch": [], "fetchrow": [], "fetchval": [], "execute": [],
        }
        self.transactions_started = 0

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._pop("fetch", sql, args, default_when_empty=[])

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._pop("fetchrow", sql, args, default_when_empty=None)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._pop("fetchval", sql, args, default_when_empty=None)

    async def execute(self, sql: str, *args: Any) -> Any:
        return self._pop("execute", sql, args, default_when_empty="OK")

    def transaction(self):
        return FakeConn._Tx(self)

    def _pop(self, kind, sql, args, *, default_when_empty):
        self.calls.append((kind, sql, args))
        queue = self.responses[kind]
        if not queue:
            return default_when_empty
        value = queue.pop(0)
        if isinstance(value, _CannedResponse):
            raise value.exc
        return value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    class _Tx:
        def __init__(self, owner):
            self._owner = owner

        async def __aenter__(self):
            self._owner.transactions_started += 1
            return self

        async def __aexit__(self, *_):
            return None


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    async def fetch(self, sql, *args):
        return await self.conn.fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        return await self.conn.fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        return await self.conn.fetchval(sql, *args)

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)

    def acquire(self):
        return self.conn


# ---------------------------------------------------------------------------
# Row builders — full column shapes for RETURNING mocks.
# ---------------------------------------------------------------------------


def _provider_v2_row(**over) -> dict:
    row = {
        "id": "google",
        "label": "Google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key_source": "GEMINI_API_KEY",
        "api_type": "google-generative-ai",
        "runtime_status": "provisioned",
        "status": "active",
        "created_at": "t0",
        "updated_at": "t0",
    }
    row.update(over)
    return row


def _base_model_v2_row(**over) -> dict:
    row = {
        "id": "google/gemini-2.5-flash",
        "provider_id": "google",
        "model_name": "gemini-2.5-flash",
        "wire_id": "google/gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "tier": "fast",
        "context_window": 1_000_000,
        "provider_shorthand": None,
        "available_context_windows": [],
        "tags": [],
        "status": "active",
        "created_at": "t0",
        "updated_at": "t0",
    }
    row.update(over)
    return row


def _valid_provider_v2_payload(**over) -> dict:
    p = {"id": "cohere", "label": "Cohere"}
    p.update(over)
    return p


def _valid_base_model_v2_payload(**over) -> dict:
    p = {
        "id": "google/gemini-2.5-flash",
        "provider_id": "google",
        "label": "Gemini 2.5 Flash",
        "tier": "fast",
        "context_window": 1_000_000,
    }
    p.update(over)
    return p


# ===========================================================================
# PROVIDER v2 — reads.
# ===========================================================================


@pytest.mark.asyncio
async def test_list_providers_v2_returns_all_columns_ordered_by_id():
    pool = FakePool()
    pool.conn.responses["fetch"] = [[_provider_v2_row(id="anthropic"),
                                      _provider_v2_row(id="google")]]
    result = await m.list_providers_v2(pool)
    assert len(result) == 2
    assert result[0]["id"] == "anthropic"
    sql = pool.conn.calls[0][1]
    assert "ORDER BY id" in sql
    assert "WHERE status" not in sql


@pytest.mark.asyncio
async def test_list_providers_v2_include_disabled_false_filters():
    pool = FakePool()
    pool.conn.responses["fetch"] = [[_provider_v2_row()]]
    await m.list_providers_v2(pool, include_disabled=False)
    assert "status = 'active'" in pool.conn.calls[0][1]


@pytest.mark.asyncio
async def test_get_provider_v2_row_or_none():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row()]
    hit = await m.get_provider_v2(pool, "google")
    assert hit is not None
    assert hit["id"] == "google"

    pool2 = FakePool()  # empty fetchrow queue → None
    assert await m.get_provider_v2(pool2, "nope") is None


# ===========================================================================
# PROVIDER v2 — create.
# ===========================================================================


@pytest.mark.asyncio
async def test_create_provider_v2_happy_path_with_defaults():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row(id="cohere", label="Cohere")]
    row = await m.create_provider_v2(pool, _valid_provider_v2_payload())
    assert row["id"] == "cohere"
    kind, sql, args = pool.conn.calls[0]
    assert kind == "fetchrow"
    assert sql.startswith("INSERT INTO model_providers")
    # positional args: id, label, base_url, api_key_source, api_type, runtime_status
    assert args[0] == "cohere"
    assert args[1] == "Cohere"
    assert args[5] == "provisioned"      # default runtime_status
    assert args[2] is None and args[3] is None and args[4] is None  # optional strings absent


@pytest.mark.asyncio
async def test_create_provider_v2_carries_optional_fields():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row()]
    # api_type must be a real one — `_validate_api_type` rejects anything the
    # gateway's schema wouldn't accept, so a placeholder like "x-api" can no
    # longer stand in here. That validation is the point: an unrecognised
    # value used to fail the whole config document on the next sync.
    await m.create_provider_v2(pool, _valid_provider_v2_payload(
        base_url="https://x", api_key_source="X_KEY",
        api_type="openai-completions",
        runtime_status="not_provisioned",
    ))
    _, _, args = pool.conn.calls[0]
    assert args[2] == "https://x"
    assert args[3] == "X_KEY"
    assert args[4] == "openai-completions"
    assert args[5] == "not_provisioned"


@pytest.mark.asyncio
async def test_create_provider_v2_duplicate_id_maps_to_409():
    pool = FakePool()
    exc = asyncpg.exceptions.UniqueViolationError("dup")
    pool.conn.responses["fetchrow"] = [raises(exc)]
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.create_provider_v2(pool, _valid_provider_v2_payload())
    assert ei.value.error_code == "duplicate_id"
    assert ei.value.status == 409


@pytest.mark.asyncio
async def test_create_provider_v2_invalid_id_rejected_before_sql():
    pool = FakePool()
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.create_provider_v2(pool, _valid_provider_v2_payload(id="Bad Caps"))
    assert ei.value.error_code == "invalid_id"
    # No SQL was built — validator fired early.
    assert pool.conn.calls == []


@pytest.mark.asyncio
async def test_create_provider_v2_missing_id_rejected():
    pool = FakePool()
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.create_provider_v2(pool, {"label": "Nameless"})
    assert ei.value.error_code == "invalid_id"


@pytest.mark.asyncio
async def test_create_provider_v2_invalid_runtime_status_rejected():
    pool = FakePool()
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.create_provider_v2(pool, _valid_provider_v2_payload(runtime_status="sometimes"))
    assert ei.value.error_code == "invalid_runtime_status"


# ===========================================================================
# PROVIDER v2 — update.
# ===========================================================================


@pytest.mark.asyncio
async def test_update_provider_v2_happy_path_sets_only_provided_fields():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row(label="New Google")]
    row = await m.update_provider_v2(pool, "google", {"label": "New Google"})
    assert row["label"] == "New Google"
    _, sql, args = pool.conn.calls[0]
    # Dynamic UPDATE builder produces exactly the touched fields + updated_at.
    assert "SET label" in sql
    assert "updated_at = now()" in sql
    # No unrelated fields ended up in the SET clause.
    assert "SET base_url" not in sql


@pytest.mark.asyncio
async def test_update_provider_v2_empty_patch_rejected():
    pool = FakePool()
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.update_provider_v2(pool, "google", {})
    assert ei.value.error_code == "empty_patch"


@pytest.mark.asyncio
async def test_update_provider_v2_invalid_patch_field_rejected_before_sql():
    pool = FakePool()
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.update_provider_v2(pool, "google", {"bogus_col": 1})
    assert ei.value.error_code == "invalid_patch_field"
    assert pool.conn.calls == []


@pytest.mark.asyncio
async def test_update_provider_v2_missing_row_maps_to_404():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [None]  # RETURNING got nothing → not found
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.update_provider_v2(pool, "ghost", {"label": "X"})
    assert ei.value.error_code == "provider_not_found"
    assert ei.value.status == 404


@pytest.mark.asyncio
async def test_update_provider_v2_optional_strings_can_be_cleared_to_null():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row(base_url=None)]
    await m.update_provider_v2(pool, "google", {"base_url": None})
    _, sql, args = pool.conn.calls[0]
    assert "SET base_url" in sql
    assert args[0] is None


# ===========================================================================
# PROVIDER v2 — set_status + orphan guard.
# ===========================================================================


@pytest.mark.asyncio
async def test_set_provider_status_v2_disable_blocked_when_active_models_reference_it():
    pool = FakePool()
    # tx step 1: SELECT ... FOR UPDATE → returns the provider row.
    # tx step 2: SELECT 1 FROM models WHERE provider_id = $1 AND status='active' → returns 1.
    pool.conn.responses["fetchrow"] = [_provider_v2_row(id="google")]
    pool.conn.responses["fetchval"] = [1]
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.set_provider_status_v2(pool, "google", "disabled")
    assert ei.value.error_code == "provider_in_use"
    assert ei.value.status == 409


@pytest.mark.asyncio
async def test_set_provider_status_v2_disable_succeeds_when_no_active_models():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        _provider_v2_row(id="anthropic"),          # SELECT FOR UPDATE
        _provider_v2_row(id="anthropic", status="disabled"),  # UPDATE ... RETURNING
    ]
    pool.conn.responses["fetchval"] = [None]  # no orphans
    row = await m.set_provider_status_v2(pool, "anthropic", "disabled")
    assert row["status"] == "disabled"
    assert pool.conn.transactions_started == 1


@pytest.mark.asyncio
async def test_set_provider_status_v2_provider_not_found_maps_to_404():
    pool = FakePool()
    # First fetchrow (SELECT FOR UPDATE) returns None.
    pool.conn.responses["fetchrow"] = [None]
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.set_provider_status_v2(pool, "ghost", "disabled")
    assert ei.value.error_code == "provider_not_found"
    assert ei.value.status == 404


@pytest.mark.asyncio
async def test_set_provider_status_v2_enable_skips_orphan_check():
    """Enabling is unrestricted — no orphan lookup should fire."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        _provider_v2_row(id="claude-cli", status="disabled"),  # FOR UPDATE
        _provider_v2_row(id="claude-cli"),                      # UPDATE RETURNING
    ]
    row = await m.set_provider_status_v2(pool, "claude-cli", "active")
    assert row["status"] == "active"
    # No fetchval (orphan-check) should have run.
    kinds = [c[0] for c in pool.conn.calls]
    assert "fetchval" not in kinds


# ===========================================================================
# BASE MODEL v2 — reads.
# ===========================================================================


@pytest.mark.asyncio
async def test_list_base_models_v2_ordered_by_id():
    pool = FakePool()
    pool.conn.responses["fetch"] = [[
        _base_model_v2_row(id="anthropic/claude-haiku-4-5"),
        _base_model_v2_row(),
    ]]
    result = await m.list_base_models_v2(pool)
    assert len(result) == 2
    assert "ORDER BY id" in pool.conn.calls[0][1]


@pytest.mark.asyncio
async def test_list_base_models_with_provider_v2_joins_and_orders_by_id():
    pool = FakePool()
    pool.conn.responses["fetch"] = [[
        {**_base_model_v2_row(id="anthropic/claude-haiku-4-5"),
         "provider_runtime_status": "provisioned", "provider_status": "active",
         "provider_credential_status": "healthy", "provider_credential_error": None},
        {**_base_model_v2_row(),
         "provider_runtime_status": "provisioned", "provider_status": "active",
         "provider_credential_status": "healthy", "provider_credential_error": None},
    ]]
    result = await m.list_base_models_with_provider_v2(pool)
    assert len(result) == 2
    sql = pool.conn.calls[0][1]
    assert "JOIN model_providers ON model_providers.id = models.provider_id" in sql
    assert "ORDER BY models.id" in sql
    assert result[0]["provider_status"] == "active"


@pytest.mark.asyncio
async def test_list_base_models_with_provider_v2_include_disabled_omits_where():
    pool = FakePool()
    pool.conn.responses["fetch"] = [[]]
    await m.list_base_models_with_provider_v2(pool, include_disabled=True)
    sql = pool.conn.calls[0][1]
    assert "WHERE models.status" not in sql


@pytest.mark.asyncio
async def test_list_base_models_with_provider_v2_default_filters_active_only():
    pool = FakePool()
    pool.conn.responses["fetch"] = [[]]
    await m.list_base_models_with_provider_v2(pool)
    sql = pool.conn.calls[0][1]
    assert "WHERE models.status = 'active'" in sql


@pytest.mark.asyncio
async def test_get_base_model_v2_row_or_none():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_base_model_v2_row()]
    assert (await m.get_base_model_v2(pool, "google/gemini-2.5-flash")) is not None
    assert (await m.get_base_model_v2(FakePool(), "nope")) is None


# ===========================================================================
# BASE MODEL v2 — create.
# ===========================================================================


@pytest.mark.asyncio
async def test_create_base_model_v2_happy_path():
    pool = FakePool()
    # fetchval inside tx: provider registered check → returns 1.
    # execute: INSERT INTO models (no windows in the default payload, so
    # no model_context_windows inserts follow).
    # fetchrow: the final SELECT that builds the return value.
    pool.conn.responses["fetchval"] = [1]
    pool.conn.responses["fetchrow"] = [_base_model_v2_row()]
    row = await m.create_base_model_v2(pool, _valid_base_model_v2_payload())
    assert row["id"] == "google/gemini-2.5-flash"
    assert pool.conn.transactions_started == 1
    # Provider FK check fired BEFORE the INSERT.
    kinds = [c[0] for c in pool.conn.calls]
    assert kinds == ["fetchval", "execute", "fetchrow"]


@pytest.mark.asyncio
async def test_create_base_model_v2_derives_model_name_from_id_ignoring_payload():
    pool = FakePool()
    pool.conn.responses["fetchval"] = [1]
    pool.conn.responses["fetchrow"] = [_base_model_v2_row()]
    payload = _valid_base_model_v2_payload()
    payload["model_name"] = "attacker-supplied"  # must be ignored
    await m.create_base_model_v2(pool, payload)
    _, _, args = pool.conn.calls[1]
    # INSERT arg order: id, provider_id, model_name, label, tier, cw, shorthand, windows
    assert args[2] == "gemini-2.5-flash"  # derived, not from payload


@pytest.mark.asyncio
async def test_create_base_model_v2_provider_not_registered_fires_before_insert():
    pool = FakePool()
    pool.conn.responses["fetchval"] = [None]  # provider check returns nothing
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload())
    assert ei.value.error_code == "provider_not_registered"
    assert ei.value.status == 409
    # No INSERT was attempted (only the fetchval check ran).
    assert [c[0] for c in pool.conn.calls] == ["fetchval"]


@pytest.mark.asyncio
async def test_create_base_model_v2_invalid_id_rejected_before_tx():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(id="no-slash-here"))
    assert ei.value.error_code == "invalid_id"
    assert pool.conn.calls == []
    assert pool.conn.transactions_started == 0


@pytest.mark.asyncio
async def test_create_base_model_v2_invalid_provider_id_rejected():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(provider_id="Bad Caps"))
    assert ei.value.error_code == "invalid_provider_id"


@pytest.mark.asyncio
async def test_create_base_model_v2_missing_provider_id_rejected():
    pool = FakePool()
    payload = _valid_base_model_v2_payload()
    del payload["provider_id"]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, payload)
    assert ei.value.error_code == "invalid_provider_id"


@pytest.mark.asyncio
async def test_create_base_model_v2_invalid_tier_rejected():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(tier="wild"))
    assert ei.value.error_code == "invalid_tier"


@pytest.mark.asyncio
async def test_create_base_model_v2_invalid_shorthand_rejected():
    pool = FakePool()
    # `bad space` still fails the relaxed shorthand regex — space is
    # not in the `[A-Za-z0-9._-]` alphabet.
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool,
            _valid_base_model_v2_payload(provider_shorthand="bad space"))
    assert ei.value.error_code == "invalid_shorthand"


@pytest.mark.asyncio
async def test_create_base_model_v2_shorthand_required_when_windows_nonempty():
    """A non-empty windows array without a shorthand raises `shorthand_required`."""
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
            available_context_windows=[100_000],  # no provider_shorthand
        ))
    assert ei.value.error_code == "shorthand_required"


@pytest.mark.asyncio
async def test_create_base_model_v2_window_exceeding_context_window_rejected():
    """A context window larger than the model's own context_window is
    rejected — a variant is a smaller slice of the native max, never larger."""
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
            context_window=1_000_000,
            provider_shorthand="flash",
            available_context_windows=[500_000, 2_000_000],
        ))
    assert ei.value.error_code == "context_window_exceeds_base"
    assert pool.conn.calls == []  # rejected before any SQL ran


@pytest.mark.asyncio
async def test_create_base_model_v2_window_equal_to_context_window_rejected():
    """A variant exactly equal to the base's context_window is rejected —
    the native size is already always offered as its own selectable
    entry (see `dropdowns.build_go_model_tree_v2`), so a same-sized
    variant would only add an indistinguishable duplicate option."""
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
            context_window=1_000_000,
            provider_shorthand="flash",
            available_context_windows=[1_000_000],
        ))
    assert ei.value.error_code == "context_window_exceeds_base"
    assert pool.conn.calls == []  # rejected before any SQL ran


@pytest.mark.asyncio
async def test_create_base_model_v2_inserts_tags():
    pool = FakePool()
    pool.conn.responses["fetchval"] = [1]
    pool.conn.responses["fetchrow"] = [
        _base_model_v2_row(tags=["reasoning", "vision"]),
    ]
    await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
        tags=["vision", "reasoning"],  # unsorted input
    ))
    tag_inserts = [
        c for c in pool.conn.calls
        if c[0] == "execute" and "INSERT INTO model_tags" in c[1]
    ]
    assert [args[1] for _, _, args in tag_inserts] == ["reasoning", "vision"]


@pytest.mark.asyncio
async def test_create_base_model_v2_unknown_tag_maps_to_400():
    pool = FakePool()
    pool.conn.responses["fetchval"] = [1]
    pool.conn.responses["execute"] = [
        "OK",  # INSERT INTO models
        raises(asyncpg.exceptions.ForeignKeyViolationError("no such tag")),
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
            tags=["not-a-real-tag"],
        ))
    assert ei.value.error_code == "unknown_tag"


@pytest.mark.asyncio
async def test_create_base_model_v2_invalid_tags_type_rejected():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
            tags="reasoning",  # not a list
        ))
    assert ei.value.error_code == "invalid_tags"
    assert pool.conn.calls == []


@pytest.mark.asyncio
async def test_update_base_model_v2_replaces_tags():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000,
         "available_context_windows": []},
        _base_model_v2_row(tags=["vision"]),
    ]
    row = await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                       {"tags": ["vision"]})
    assert row["tags"] == ["vision"]
    execute_calls = [c for c in pool.conn.calls if c[0] == "execute"]
    assert any("DELETE FROM model_tags" in sql for _, sql, _ in execute_calls)
    assert any("INSERT INTO model_tags" in sql for _, sql, _ in execute_calls)


@pytest.mark.asyncio
async def test_update_base_model_v2_clears_tags_with_empty_list():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000,
         "available_context_windows": []},
        _base_model_v2_row(tags=[]),
    ]
    row = await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                       {"tags": []})
    assert row["tags"] == []
    execute_calls = [c for c in pool.conn.calls if c[0] == "execute"]
    # DELETE runs (clearing), no INSERT follows for an empty tag list.
    assert any("DELETE FROM model_tags" in sql for _, sql, _ in execute_calls)
    assert not any("INSERT INTO model_tags" in sql for _, sql, _ in execute_calls)


@pytest.mark.asyncio
async def test_list_tags_v2_returns_all_rows_ordered():
    pool = FakePool()
    pool.conn.responses["fetch"] = [[
        {"name": "vision", "description": "..."},
        {"name": "reasoning", "description": "..."},
    ]]
    rows = await m.list_tags_v2(pool)
    assert [r["name"] for r in rows] == ["vision", "reasoning"]
    assert "ORDER BY name" in pool.conn.calls[0][1]


@pytest.mark.asyncio
async def test_create_base_model_v2_duplicate_id_maps_to_409():
    pool = FakePool()
    pool.conn.responses["fetchval"] = [1]
    # The UniqueViolationError now fires on the `models` INSERT itself
    # (an `execute` call), not on a RETURNING fetchrow.
    pool.conn.responses["execute"] = [raises(asyncpg.exceptions.UniqueViolationError("dup"))]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload())
    assert ei.value.error_code == "duplicate_id"
    assert ei.value.status == 409


@pytest.mark.asyncio
async def test_create_base_model_v2_normalises_windows_array():
    """Each window is inserted into model_context_windows in sorted order."""
    pool = FakePool()
    pool.conn.responses["fetchval"] = [1]
    pool.conn.responses["fetchrow"] = [_base_model_v2_row(
        provider_shorthand="flash",
        available_context_windows=[100_000, 200_000, 500_000],
    )]
    await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
        provider_shorthand="flash",
        available_context_windows=[500_000, 100_000, 200_000],  # unsorted
    ))
    # calls[0] = fetchval (provider check), calls[1] = execute (INSERT
    # INTO models), calls[2:5] = one execute per context window.
    window_inserts = [c for c in pool.conn.calls if c[0] == "execute"][1:]
    inserted_tokens = [args[1] for _, _, args in window_inserts]
    assert inserted_tokens == [100_000, 200_000, 500_000]


@pytest.mark.asyncio
async def test_create_base_model_v2_duplicate_windows_rejected():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.create_base_model_v2(pool, _valid_base_model_v2_payload(
            provider_shorthand="flash",
            available_context_windows=[100_000, 100_000],
        ))
    assert ei.value.error_code == "invalid_available_context_windows"


# ===========================================================================
# BASE MODEL v2 — update.
# ===========================================================================


@pytest.mark.asyncio
async def test_update_base_model_v2_happy_path_updates_only_touched_fields():
    pool = FakePool()
    # tx step 1: SELECT ... FOR UPDATE returns current state.
    # tx step 2: UPDATE ... RETURNING returns the new row.
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": None, "context_window": 1_000_000, "available_context_windows": []},
        _base_model_v2_row(label="New Name"),
    ]
    row = await m.update_base_model_v2(pool, "google/gemini-2.5-flash", {"label": "New Name"})
    assert row["label"] == "New Name"
    assert pool.conn.transactions_started == 1


@pytest.mark.asyncio
async def test_update_base_model_v2_empty_patch_rejected():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash", {})
    assert ei.value.error_code == "empty_patch"


@pytest.mark.asyncio
async def test_update_base_model_v2_invalid_patch_field_rejected_before_tx():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash", {"nope": 1})
    assert ei.value.error_code == "invalid_patch_field"
    assert pool.conn.transactions_started == 0


@pytest.mark.asyncio
async def test_update_base_model_v2_model_not_found_maps_to_404():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [None]  # SELECT FOR UPDATE returned nothing
    with pytest.raises(m.ModelValidationError) as ei:
        await m.update_base_model_v2(pool, "ghost/model", {"label": "X"})
    assert ei.value.error_code == "model_not_found"
    assert ei.value.status == 404


@pytest.mark.asyncio
async def test_update_base_model_v2_shorthand_required_on_nonempty_windows_update():
    """Setting non-empty windows on a row whose shorthand is NULL — and NOT
    supplying a shorthand in the same patch — raises `shorthand_required`
    against the merged post-patch state."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": None, "context_window": 1_000_000, "available_context_windows": []},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                     {"available_context_windows": [100_000]})
    assert ei.value.error_code == "shorthand_required"


@pytest.mark.asyncio
async def test_update_base_model_v2_shorthand_and_windows_together_ok():
    """The same patch supplying BOTH shorthand and windows is fine."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": None, "context_window": 1_000_000, "available_context_windows": []},
        _base_model_v2_row(provider_shorthand="flash",
                           available_context_windows=[100_000]),
    ]
    row = await m.update_base_model_v2(pool, "google/gemini-2.5-flash", {
        "provider_shorthand": "flash",
        "available_context_windows": [100_000],
    })
    assert row["provider_shorthand"] == "flash"


@pytest.mark.asyncio
async def test_update_base_model_v2_window_exceeding_current_context_window_rejected():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000,
         "available_context_windows": []},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                     {"available_context_windows": [2_000_000]})
    assert ei.value.error_code == "context_window_exceeds_base"


@pytest.mark.asyncio
async def test_update_base_model_v2_shrinking_context_window_below_existing_variant_rejected():
    """Shrinking context_window itself below an already-stored variant size
    is caught too — checked against the MERGED post-patch state, not just
    the value being patched."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000,
         "available_context_windows": [500_000, 1_000_000]},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                     {"context_window": 200_000})
    assert ei.value.error_code == "context_window_exceeds_base"


@pytest.mark.asyncio
async def test_update_base_model_v2_setting_shorthand_null_with_populated_windows_rejected():
    """Clearing the shorthand while windows are non-empty violates the invariant."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000, "available_context_windows": [100_000]},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                     {"provider_shorthand": None})
    assert ei.value.error_code == "shorthand_required"


# ===========================================================================
# BASE MODEL v2 — set_status.
# ===========================================================================


@pytest.mark.asyncio
async def test_set_base_model_status_v2_toggles_via_update():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000, "available_context_windows": [100_000]},
        _base_model_v2_row(status="disabled"),
    ]
    row = await m.set_base_model_status_v2(pool, "google/gemini-2.5-flash", "disabled")
    assert row["status"] == "disabled"


@pytest.mark.asyncio
async def test_set_base_model_status_v2_invalid_status_rejected():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.set_base_model_status_v2(pool, "google/gemini-2.5-flash", "wat")
    assert ei.value.error_code == "invalid_status"


# ===========================================================================
# CONTEXT WINDOWS — add.
# ===========================================================================


@pytest.mark.asyncio
async def test_add_context_window_v2_happy_path():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000},
        _base_model_v2_row(provider_shorthand="flash",
                           available_context_windows=[100_000, 200_000]),
    ]
    row = await m.add_context_window_v2(pool, "google/gemini-2.5-flash", 200_000)
    assert row["available_context_windows"] == [100_000, 200_000]
    assert pool.conn.transactions_started == 1
    # Only touches model_context_windows — no models/model_providers row
    # for the variant (see the function's docstring for why not).
    execute_kinds = [sql for kind, sql, _ in pool.conn.calls if kind == "execute"]
    assert execute_kinds == ["INSERT INTO model_context_windows (model_id, tokens) VALUES ($1, $2)"]


@pytest.mark.asyncio
async def test_add_context_window_v2_sorts_the_new_array_ascending():
    """The returned row's windows come back sorted — real sort ordering
    now lives in the SQL (ORDER BY in the model_context_windows
    subquery), not app code; this asserts the INSERT itself carries the
    right (model_id, tokens) pair and the function returns whatever the
    final SELECT reports."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000},
        _base_model_v2_row(provider_shorthand="flash",
                           available_context_windows=[100_000, 200_000, 500_000]),
    ]
    row = await m.add_context_window_v2(pool, "google/gemini-2.5-flash", 100_000)
    kind, sql, args = pool.conn.calls[1]
    assert kind == "execute"
    assert "INSERT INTO model_context_windows" in sql
    assert args == ("google/gemini-2.5-flash", 100_000)
    assert row["available_context_windows"] == [100_000, 200_000, 500_000]


@pytest.mark.asyncio
async def test_add_context_window_v2_duplicate_maps_to_409():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [{"provider_shorthand": "flash", "context_window": 1_000_000}]
    pool.conn.responses["execute"] = [
        raises(asyncpg.exceptions.UniqueViolationError("dup")),
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.add_context_window_v2(pool, "google/gemini-2.5-flash", 100_000)
    assert ei.value.error_code == "duplicate_window"
    assert ei.value.status == 409


@pytest.mark.asyncio
async def test_add_context_window_v2_shorthand_required_when_null():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": None},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.add_context_window_v2(pool, "claude-cli/opus", 100_000)
    assert ei.value.error_code == "shorthand_required"
    assert ei.value.status == 400


@pytest.mark.asyncio
async def test_add_context_window_v2_invalid_tokens_maps_to_400_before_tx():
    pool = FakePool()
    with pytest.raises(m.ModelValidationError) as ei:
        await m.add_context_window_v2(pool, "google/gemini-2.5-flash", 999)
    assert ei.value.error_code == "invalid_tokens"
    assert ei.value.status == 400
    assert pool.conn.transactions_started == 0


@pytest.mark.asyncio
async def test_add_context_window_v2_exceeding_base_context_window_rejected():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.add_context_window_v2(pool, "google/gemini-2.5-flash", 2_000_000)
    assert ei.value.error_code == "context_window_exceeds_base"
    assert ei.value.status == 400
    # No junction-table write attempted.
    assert [c for c in pool.conn.calls if c[0] == "execute"] == []


@pytest.mark.asyncio
async def test_add_context_window_v2_equal_to_base_context_window_rejected():
    """tokens exactly equal to the base's own context_window is rejected
    too, not just greater — the native size is already always offered
    as its own selectable entry (see `dropdowns.build_go_model_tree_v2`),
    so a same-sized variant would only add an indistinguishable
    duplicate option in the size picker."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"provider_shorthand": "flash", "context_window": 1_000_000},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.add_context_window_v2(pool, "google/gemini-2.5-flash", 1_000_000)
    assert ei.value.error_code == "context_window_exceeds_base"
    assert ei.value.status == 400
    assert [c for c in pool.conn.calls if c[0] == "execute"] == []


@pytest.mark.asyncio
async def test_add_context_window_v2_model_not_found_maps_to_404():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [None]  # SELECT FOR UPDATE misses
    with pytest.raises(m.ModelValidationError) as ei:
        await m.add_context_window_v2(pool, "ghost/model", 100_000)
    assert ei.value.error_code == "model_not_found"
    assert ei.value.status == 404


# ===========================================================================
# CONTEXT WINDOWS — remove.
# ===========================================================================


@pytest.mark.asyncio
async def test_remove_context_window_v2_happy_path():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"id": "google/gemini-2.5-flash"},
        _base_model_v2_row(provider_shorthand="flash",
                           available_context_windows=[100_000, 500_000]),
    ]
    pool.conn.responses["fetchval"] = [200_000]  # DELETE ... RETURNING tokens
    row = await m.remove_context_window_v2(pool, "google/gemini-2.5-flash", 200_000)
    assert row["available_context_windows"] == [100_000, 500_000]
    assert pool.conn.transactions_started == 1
    # Only touches model_context_windows — no models row to disable for
    # a variant that never had one (see the function's docstring).
    assert [c for c in pool.conn.calls if c[0] == "execute"] == []


@pytest.mark.asyncio
async def test_remove_context_window_v2_window_not_present_maps_to_404():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"available_context_windows": [100_000, 200_000]},
    ]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.remove_context_window_v2(pool, "google/gemini-2.5-flash", 500_000)
    assert ei.value.error_code == "window_not_present"
    assert ei.value.status == 404


@pytest.mark.asyncio
async def test_remove_context_window_v2_model_not_found_maps_to_404():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [None]
    with pytest.raises(m.ModelValidationError) as ei:
        await m.remove_context_window_v2(pool, "ghost/model", 100_000)
    assert ei.value.error_code == "model_not_found"
    assert ei.value.status == 404


# ===========================================================================
# Credential status: gateway-report path (sticky against 'revoked') +
# admin-asserted revoke.
# ===========================================================================


@pytest.mark.asyncio
async def test_set_provider_credential_status_v2_writes_healthy():
    pool = FakePool()
    pool.conn.responses["execute"] = ["UPDATE 1"]
    await m.set_provider_credential_status_v2(pool, "anthropic", healthy=True)
    kind, sql, args = pool.conn.calls[0]
    assert kind == "execute"
    assert "credential_status != 'revoked'" in sql
    assert args == ("anthropic", "healthy", None)


@pytest.mark.asyncio
async def test_set_provider_credential_status_v2_writes_unhealthy_with_error():
    pool = FakePool()
    pool.conn.responses["execute"] = ["UPDATE 1"]
    await m.set_provider_credential_status_v2(
        pool, "anthropic", healthy=False, error="permission denied",
    )
    args = pool.conn.calls[0][2]
    assert args == ("anthropic", "unhealthy", "permission denied")


@pytest.mark.asyncio
async def test_set_provider_credential_status_v2_logs_but_does_not_raise_when_sticky_guard_fires(caplog):
    """UPDATE 0 covers both 'no such row' and 'row is revoked, guard
    fired' — either way this must not raise, since callers are relaying
    a gateway report, not performing an admin action."""
    pool = FakePool()
    pool.conn.responses["execute"] = ["UPDATE 0"]
    await m.set_provider_credential_status_v2(pool, "anthropic", healthy=True)  # must not raise


@pytest.mark.asyncio
async def test_revoke_provider_credential_v2_sets_revoked():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        {"id": "anthropic", "credential_status": "revoked", "credential_checked_at": "t1"},
    ]
    result = await m.revoke_provider_credential_v2(pool, "anthropic")
    assert result["credential_status"] == "revoked"
    sql, args = pool.conn.calls[0][1], pool.conn.calls[0][2]
    assert "credential_status = 'revoked'" in sql
    assert "credential_error = NULL" in sql
    assert args == ("anthropic",)


@pytest.mark.asyncio
async def test_revoke_provider_credential_v2_raises_not_found():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [None]
    with pytest.raises(m.ProviderValidationError) as ei:
        await m.revoke_provider_credential_v2(pool, "ghost-provider")
    assert ei.value.error_code == "not_found"


@pytest.mark.asyncio
async def test_update_provider_v2_rotating_secret_id_resets_credential_status():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [
        _provider_v2_row(secret_id="new-secret-name", credential_status="unknown"),
    ]
    await m.update_provider_v2(pool, "google", {"secret_id": "new-secret-name"})
    sql = pool.conn.calls[0][1]
    assert "credential_status = 'unknown'" in sql
    assert "credential_error = NULL" in sql
    assert "credential_checked_at = NULL" in sql


@pytest.mark.asyncio
async def test_update_provider_v2_not_touching_secret_id_leaves_credential_status_alone():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row(label="Google Renamed")]
    await m.update_provider_v2(pool, "google", {"label": "Google Renamed"})
    sql = pool.conn.calls[0][1]
    set_clause = sql.split("RETURNING")[0]  # credential_status legitimately
    # appears in the RETURNING column list — only the SET clause matters here.
    assert "credential_status = " not in set_clause


# ---------------------------------------------------------------------------
# api_type validation (added 2026-08-09).
#
# An unrecognised api_type is not a cosmetic data-quality issue: the sync
# writes the whole config document in one config.set, so a single bad value
# fails validation for EVERY provider and the soft-fail swallows the error —
# provider sync stops platform-wide until someone reads the logs. These pin
# that it can no longer be stored.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_provider_v2_rejects_api_type_the_gateway_would_refuse():
    pool = FakePool()
    for bad in ("azure-openai-responses", "google-vertex",
                "mistral-conversations", "google-gemini-cli", "totally-made-up"):
        with pytest.raises(m.ProviderValidationError) as exc:
            await m.create_provider_v2(pool, _valid_provider_v2_payload(api_type=bad))
        assert exc.value.error_code == "invalid_api_type"


@pytest.mark.asyncio
async def test_create_provider_v2_accepts_cli_even_though_the_gateway_does_not():
    """`cli` marks a CLI-backed provider. It is meaningful to AgentOS and
    unknown to the gateway — which is fine, because such providers have no
    base_url and are never emitted."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row()]
    await m.create_provider_v2(pool, _valid_provider_v2_payload(api_type="cli"))


@pytest.mark.asyncio
async def test_create_provider_v2_allows_null_api_type():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_provider_v2_row()]
    await m.create_provider_v2(pool, _valid_provider_v2_payload())


@pytest.mark.asyncio
async def test_update_provider_v2_rejects_a_bad_api_type_too():
    """The create path is not the only way in — patching must validate as
    well, or an existing provider can still be walked into the broken state."""
    pool = FakePool()
    with pytest.raises(m.ProviderValidationError) as exc:
        await m.update_provider_v2(pool, "anthropic", {"api_type": "google-vertex"})
    assert exc.value.error_code == "invalid_api_type"


# ---------------------------------------------------------------------------
# context_window_exceeds_base must not fire on patches that don't touch it
# (regression, 2026-08-13).
#
# The guard re-reads the model's CURRENT windows and context_window whenever the
# patch omits them, so evaluating it unconditionally judged a patch against data
# it wasn't changing. Four seeded base models carry a window >= their own native
# size (the seed migrations INSERT into model_context_windows directly, bypassing
# add_context_window_v2's equivalent guard), so every edit of those four failed
# with a 400 — including disable/enable, which routes through this function.
# Verified against live feature data before the fix.
# ---------------------------------------------------------------------------


def _violating_model_row(**over) -> dict:
    """A base model already on the wrong side of the invariant: its largest
    configured window equals its native context_window. Matches the real seeded
    state of gemini-2.5-pro, gemini-2.5-flash, claude-opus-4-6 and (worse)
    claude-sonnet-4-5, whose windows exceed its native size."""
    return _base_model_v2_row(
        id="google/gemini-2.5-pro", provider_shorthand="pro",
        context_window=1_000_000,
        available_context_windows=[100_000, 200_000, 500_000, 1_000_000],
        **over,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"label": "Renamed"},
    {"tier": "premium"},
    {"tags": ["reasoning"]},
    {"provider_shorthand": "pro2"},
    {"status": "disabled"},
])
async def test_update_base_model_v2_allows_patches_that_dont_touch_windows(patch):
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_violating_model_row(), _violating_model_row()]
    # Must not raise: the pre-existing violation is untouched by these patches.
    await m.update_base_model_v2(pool, "google/gemini-2.5-pro", patch)


@pytest.mark.asyncio
async def test_set_base_model_status_v2_not_blocked_by_preexisting_violation():
    """Disable/enable delegates to update_base_model_v2, so it hit the same
    guard — an admin could not disable an affected model at all."""
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_violating_model_row(), _violating_model_row()]
    await m.set_base_model_status_v2(pool, "google/gemini-2.5-pro", "disabled")


@pytest.mark.asyncio
async def test_update_base_model_v2_still_rejects_adding_an_oversized_window():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_base_model_v2_row(
        provider_shorthand="flash", context_window=1_000_000,
        available_context_windows=[100_000],
    )]
    with pytest.raises(m.ModelValidationError) as exc:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                     {"available_context_windows": [5_000_000]})
    assert exc.value.error_code == "context_window_exceeds_base"


@pytest.mark.asyncio
async def test_update_base_model_v2_still_rejects_shrinking_base_below_a_variant():
    pool = FakePool()
    pool.conn.responses["fetchrow"] = [_base_model_v2_row(
        provider_shorthand="flash", context_window=1_000_000,
        available_context_windows=[500_000],
    )]
    with pytest.raises(m.ModelValidationError) as exc:
        await m.update_base_model_v2(pool, "google/gemini-2.5-flash",
                                     {"context_window": 100_000})
    assert exc.value.error_code == "context_window_exceeds_base"
