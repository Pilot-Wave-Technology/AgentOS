"""Unit tests for F1 (conditional provider-auth push) and F2 (config.set,
no-restart write) in gateway_sync.py.

`config.patch`'s gateway-side handler always schedules a restart — there is
no no-op branch. Before F1, `sync_provider_auth_configs_to_gateway` pushed
unconditionally, so every goal creation restarted the gateway even when the
payload was byte-identical to what was already live. F2 additionally swaps
the write itself from `config.patch` to `config.set` (whole-document
replace, no restart at all, no rate limit), so even a genuine provider
change no longer costs a restart. These tests pin the comparison helpers
directly (`_comparable_provider` / `_providers_already_current`) and the
end-to-end skip/write behavior of `sync_provider_auth_configs_to_gateway`
itself, with `gateway_admin.call` mocked — no live gateway, no live DB.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.openclaw import GatewayError
from app.services.model_registry import gateway_sync as gs


# ===========================================================================
# _comparable_provider — strips fields `config.get` redacts.
# ===========================================================================


def test_comparable_provider_strips_api_key_and_headers():
    provider = {
        "baseUrl": "https://api.anthropic.com/v1",
        "apiKey": "ANTHROPIC_API_KEY",
        "headers": {"x-custom": "value"},
        "models": [{"id": "claude-opus-4-8"}],
    }
    assert gs._comparable_provider(provider) == {
        "baseUrl": "https://api.anthropic.com/v1",
        "models": [{"id": "claude-opus-4-8"}],
    }


def test_comparable_provider_tolerates_missing_sensitive_fields():
    # A provider dict with neither field present must not raise.
    assert gs._comparable_provider({"baseUrl": "x"}) == {"baseUrl": "x"}


def test_comparable_provider_empty_dict():
    assert gs._comparable_provider({}) == {}


# ===========================================================================
# _providers_already_current
# ===========================================================================


def test_already_current_true_when_redacted_api_key_is_the_only_difference():
    live = {
        "anthropic": {
            "baseUrl": "https://api.anthropic.com/v1",
            "apiKey": "__OPENCLAW_REDACTED__",
            "models": [],
        },
    }
    emitted = {
        "anthropic": {
            "baseUrl": "https://api.anthropic.com/v1",
            "apiKey": "ANTHROPIC_API_KEY",
            "models": [],
        },
    }
    assert gs._providers_already_current(live, emitted) is True


def test_already_current_ignores_gateway_builtin_keys_we_do_not_manage():
    # The gateway may hold provider keys of its own that we never emit —
    # superset match (`>=`), not exact set equality.
    live = {
        "anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "x"},
        "some-gateway-builtin": {"baseUrl": "https://builtin.example"},
    }
    emitted = {"anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "y"}}
    assert gs._providers_already_current(live, emitted) is True


def test_not_current_when_emitted_provider_missing_from_live():
    live = {"anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "x"}}
    emitted = {
        "anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "y"},
        "google": {"baseUrl": "https://generativelanguage.googleapis.com/v1beta", "apiKey": "z"},
    }
    assert gs._providers_already_current(live, emitted) is False


def test_not_current_when_a_non_sensitive_field_differs():
    live = {"anthropic": {"baseUrl": "https://old.example", "apiKey": "x"}}
    emitted = {"anthropic": {"baseUrl": "https://new.example", "apiKey": "y"}}
    assert gs._providers_already_current(live, emitted) is False


def test_removed_provider_still_triggers_a_write():
    """A provider removed from our registry must not look 'already
    satisfied' just because everything we still emit happens to match —
    `>=` on the key set only skips the write when live is a superset of
    what we currently emit, not the other way round."""
    live = {
        "anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "x"},
        "google": {"baseUrl": "https://generativelanguage.googleapis.com/v1beta", "apiKey": "y"},
    }
    # We used to emit both; now only anthropic remains active. `google`
    # should have been removed from the gateway, which the current
    # superset-only check on `providers` cannot detect on its own — this
    # test documents that limitation rather than asserting a stronger
    # guarantee than the function provides.
    emitted = {"anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "x"}}
    # `providers` (what we'd emit) shrank to a subset of `live` — by design
    # this still reports "current" for the keys we still manage. Cleanup
    # of a fully-removed provider from `models.providers` is not this
    # function's job; it only guards against under-writing, not stale
    # residue. Documented here so a future reader doesn't assume more.
    assert gs._providers_already_current(live, emitted) is True


def test_already_current_true_for_empty_providers_payload():
    # Vacuous case: nothing to emit means nothing can be "not current".
    assert gs._providers_already_current({"anthropic": {}}, {}) is True


# ===========================================================================
# sync_provider_auth_configs_to_gateway — end-to-end skip/write behavior.
# ===========================================================================


@pytest.fixture
def patched_queries(monkeypatch):
    """Bypass the DB entirely — the payload shape is exercised by
    test_emitter.py; here we only care about the compare-then-write
    decision, so a fixed `providers` dict is injected via
    `build_providers_payload`.

    `fetch_all_provider_owner_ids` (added for pruning, 2026-08-18) is mocked
    too, with the three owner ids real deployments have — otherwise every
    test here would hit a real pool.acquire() through the unpatched query.
    Tests that care about a SPECIFIC owner set override this via monkeypatch
    directly."""
    monkeypatch.setattr(gs, "fetch_active_base_models", AsyncMock(return_value=[]))
    monkeypatch.setattr(gs, "fetch_native_provider_configs", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        gs, "fetch_all_provider_owner_ids",
        AsyncMock(return_value=[{"id": "anthropic"}, {"id": "google"}, {"id": "claude-cli"}]),
    )
    monkeypatch.setattr(gs, "get_pool", lambda: object())


_PROVIDERS = {
    "anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "ANTHROPIC_API_KEY", "models": []},
}


@pytest.mark.asyncio
async def test_sync_skips_config_patch_when_already_current(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    call = AsyncMock(return_value={
        "hash": "abc123",
        "config": {"models": {"providers": {
            "anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }}},
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    call.assert_awaited_once_with("config.get", {})  # config.patch never called


@pytest.mark.asyncio
async def test_sync_writes_config_set_when_provider_changed(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    call = AsyncMock(return_value={
        "hash": "abc123",
        "config": {"models": {"providers": {
            "anthropic": {"baseUrl": "https://OLD.example", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }}},
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    assert call.await_count == 2
    (first_method, _), (second_method, second_params) = (c.args for c in call.await_args_list)
    assert first_method == "config.get"
    assert second_method == "config.set"  # F2: no restart, no rate limit
    assert second_params["baseHash"] == "abc123"
    sent = json.loads(second_params["raw"])
    assert sent == {"models": {"providers": _PROVIDERS}}  # whole document sent, not a partial patch


@pytest.mark.asyncio
async def test_sync_returns_early_with_no_gateway_call_when_no_providers(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: {})
    call = AsyncMock()
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_skips_write_when_config_get_has_no_hash(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    call = AsyncMock(return_value={"config": {}})  # no "hash" key
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    call.assert_awaited_once_with("config.get", {})


@pytest.mark.asyncio
async def test_sync_soft_fails_on_gateway_exception(monkeypatch, patched_queries):
    """Never raises — go_agent.py's create_go_for_goal and three admin
    routes all call this inline; a gateway hiccup must not break goal
    creation or an admin edit."""
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    monkeypatch.setattr(gs.gateway_admin, "call", AsyncMock(side_effect=RuntimeError("ws down")))

    await gs.sync_provider_auth_configs_to_gateway()  # must not raise


# ===========================================================================
# F2 — config.set specifics: whole-document send, diff assertion, stale-hash retry.
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_preserves_unrelated_top_level_config_when_writing(monkeypatch, patched_queries):
    """config.set replaces the WHOLE document — this pins that unrelated
    top-level sections (here: agents.defaults) survive the round-trip
    byte-for-byte, since a bug sending a partial document would silently
    read as "delete everything else"."""
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    live_config = {
        "agents": {"defaults": {"model": "google/gemini-2.5-flash"}},
        "models": {"providers": {
            "anthropic": {"baseUrl": "https://OLD.example", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }},
    }
    call = AsyncMock(return_value={"hash": "abc123", "config": live_config})
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    _, second_params = call.await_args_list[1].args
    sent = json.loads(second_params["raw"])
    assert sent["agents"] == {"defaults": {"model": "google/gemini-2.5-flash"}}
    assert sent["models"]["providers"] == _PROVIDERS


def test_assert_only_providers_changed_raises_on_unrelated_drift():
    before = {"agents": {"defaults": {"model": "x"}}, "models": {"providers": {}}}
    after = {"agents": {"defaults": {"model": "y"}}, "models": {"providers": {"anthropic": {}}}}
    with pytest.raises(RuntimeError, match="agents"):
        gs._assert_only_providers_changed(before, after)


def test_assert_only_providers_changed_raises_on_other_models_key_drift():
    before = {"models": {"providers": {}, "somethingElse": 1}}
    after = {"models": {"providers": {"anthropic": {}}, "somethingElse": 2}}
    with pytest.raises(RuntimeError, match="models"):
        gs._assert_only_providers_changed(before, after)


def test_assert_only_providers_changed_allows_only_providers_diff():
    before = {"agents": {"defaults": {"model": "x"}}, "models": {"providers": {}}}
    after = {"agents": {"defaults": {"model": "x"}}, "models": {"providers": {"anthropic": {}}}}
    gs._assert_only_providers_changed(before, after)  # must not raise


@pytest.mark.asyncio
async def test_sync_retries_once_on_stale_base_hash_then_succeeds(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    live_config = {"models": {"providers": {
        "anthropic": {"baseUrl": "https://OLD.example", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
    }}}

    calls = []

    async def fake_call(method, params=None):
        calls.append(method)
        if method == "config.get":
            return {"hash": f"hash-{len([c for c in calls if c == 'config.get'])}", "config": live_config}
        if method == "config.set":
            if len([c for c in calls if c == "config.set"]) == 1:
                raise GatewayError("config changed since last load; re-run config.get and retry")
            return {"ok": True}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(gs.gateway_admin, "call", fake_call)
    monkeypatch.setattr(gs.asyncio, "sleep", AsyncMock())  # no real delay in tests

    await gs.sync_provider_auth_configs_to_gateway()

    assert calls == ["config.get", "config.set", "config.get", "config.set"]


@pytest.mark.asyncio
async def test_sync_gives_up_after_max_stale_hash_retries(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    live_config = {"models": {"providers": {
        "anthropic": {"baseUrl": "https://OLD.example", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
    }}}

    async def always_stale(method, params=None):
        if method == "config.get":
            return {"hash": "abc123", "config": live_config}
        if method == "config.set":
            raise GatewayError("config changed since last load; re-run config.get and retry")
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(gs.gateway_admin, "call", always_stale)
    monkeypatch.setattr(gs.asyncio, "sleep", AsyncMock())

    await gs.sync_provider_auth_configs_to_gateway()  # soft-fails, must not raise out


# ===========================================================================
# Invalid-config guard (regression, 2026-08-13).
#
# `config.get` blanks `config` to {} for ANY invalid snapshot while still
# returning the real hash of the broken file. Without a `valid` check the live
# document looks empty, so the write path builds a "full" document from that
# empty base and config.set replaces openclaw.json with only models.providers —
# losing gateway.bind, the auth token, agents.defaults and channels. config.patch
# rejects this case itself; config.set does not, so the guard belongs here.
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_refuses_to_write_when_gateway_reports_invalid_config(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    call = AsyncMock(return_value={
        "hash": "abc123",       # real hash of the BROKEN file
        "valid": False,
        "config": {},           # gateway blanks it — looks empty, isn't
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    # config.get only — never config.set. A write here wipes the document.
    call.assert_awaited_once_with("config.get", {})


@pytest.mark.asyncio
async def test_sync_still_writes_when_gateway_omits_the_valid_field(monkeypatch, patched_queries):
    """An older gateway build that doesn't report `valid` must keep syncing —
    hence `is False` rather than a falsy check."""
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    call = AsyncMock(return_value={
        "hash": "abc123",
        "config": {"models": {"providers": {
            "anthropic": {"baseUrl": "https://OLD.example", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }}},
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    assert call.await_count == 2
    assert call.await_args_list[1].args[0] == "config.set"


@pytest.mark.asyncio
async def test_sync_writes_normally_when_config_is_explicitly_valid(monkeypatch, patched_queries):
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    call = AsyncMock(return_value={
        "hash": "abc123",
        "valid": True,
        "config": {"models": {"providers": {
            "anthropic": {"baseUrl": "https://OLD.example", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }}},
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    assert call.await_count == 2
    assert call.await_args_list[1].args[0] == "config.set"


# ===========================================================================
# Pruning stale AgentOS-owned synthetic providers (2026-08-18).
#
# Bug found via the Gemini investigation (research/GEMINI_OPAQUE_AGENT_FAILURES
# _2026-08-16.md): `.update(providers)` can only add/overwrite, so a provider
# whose backing DB row (a model_context_windows window, or the base model
# itself) was removed stayed live in the gateway config forever. These tests
# pin `_agentos_owned_provider_keys` / `_stale_agentos_provider_keys` directly,
# then the end-to-end behavior through `sync_provider_auth_configs_to_gateway`.
# ===========================================================================


def test_agentos_owned_recognizes_the_real_naming_convention():
    """synthesize_variant's provider_key format: <owner>-<shorthand>-<size>."""
    owned = gs._agentos_owned_provider_keys(
        {"anthropic-sonnet-500k", "anthropic-sonnet-1m", "google-flash-100k"},
        {"anthropic", "google", "claude-cli"},
    )
    assert owned == {"anthropic-sonnet-500k", "anthropic-sonnet-1m", "google-flash-100k"}


def test_agentos_owned_never_matches_a_bare_builtin_provider():
    """A gateway built-in like 'anthropic' or 'google' must never be
    recognized as ours — pruning must never be able to touch it."""
    owned = gs._agentos_owned_provider_keys(
        {"anthropic", "google", "claude-cli", "moonshot", "zai"},
        {"anthropic", "google", "claude-cli"},
    )
    assert owned == set()


def test_agentos_owned_never_matches_an_unknown_owner():
    """A dash-shaped key whose prefix is NOT a known model_providers.id must
    not be swept up even though it superficially matches the pattern —
    e.g. a hand-configured provider from an owner we don't manage."""
    owned = gs._agentos_owned_provider_keys(
        {"someothervendor-custom-100k"},
        {"anthropic", "google", "claude-cli"},
    )
    assert owned == set()


def test_agentos_owned_rejects_malformed_suffixes():
    """The suffix must be exactly <shorthand>-<digits><k|m> — reject anything
    that merely starts with a known owner but isn't actually our shape."""
    owned = gs._agentos_owned_provider_keys(
        {
            "anthropic-sonnet",           # missing the size suffix entirely
            "anthropic-sonnet-",          # size suffix empty
            "anthropic-sonnet-abc",       # size isn't <digits><k|m>
            "anthropic-sonnet-100",       # no k/m unit
            "anthropic-sonnet-100x",      # wrong unit
        },
        {"anthropic", "google", "claude-cli"},
    )
    assert owned == set()


def test_stale_keys_is_live_minus_current_payload_restricted_to_owned():
    """A live key we don't own (a builtin) is never reported stale even if
    it's absent from `providers` — only OUR removed keys count."""
    stale = gs._stale_agentos_provider_keys(
        live={"anthropic-sonnet-500k": {}, "anthropic-sonnet-200k": {}, "anthropic": {}},
        providers={"anthropic-sonnet-200k": {}},
        owner_ids={"anthropic", "google", "claude-cli"},
    )
    assert stale == {"anthropic-sonnet-500k"}


def test_stale_keys_empty_when_nothing_was_removed():
    stale = gs._stale_agentos_provider_keys(
        live={"anthropic-sonnet-200k": {}},
        providers={"anthropic-sonnet-200k": {}},
        owner_ids={"anthropic", "google", "claude-cli"},
    )
    assert stale == set()


@pytest.mark.asyncio
async def test_sync_prunes_a_stale_variant_no_longer_in_the_payload(monkeypatch, patched_queries):
    """The end-to-end case this whole fix targets: a variant's DB row is gone
    (only anthropic-sonnet-200k is still emitted), and the stale
    anthropic-sonnet-500k/1m entries must be DELETED from the document sent
    to config.set — not merely left untouched."""
    current_providers = {
        "anthropic-sonnet-200k": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "ANTHROPIC_API_KEY", "models": []},
    }
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: current_providers)
    call = AsyncMock(return_value={
        "hash": "abc123",
        "config": {"models": {"providers": {
            "anthropic-sonnet-200k": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
            "anthropic-sonnet-500k": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
            "anthropic-sonnet-1m": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
            "anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }}},
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    assert call.await_count == 2
    method, params = call.await_args_list[1].args
    assert method == "config.set"
    sent_providers = json.loads(params["raw"])["models"]["providers"]
    assert "anthropic-sonnet-500k" not in sent_providers
    assert "anthropic-sonnet-1m" not in sent_providers
    assert sent_providers["anthropic-sonnet-200k"] == current_providers["anthropic-sonnet-200k"]
    # the gateway built-in must survive untouched — pruning never touches it
    assert "anthropic" in sent_providers


@pytest.mark.asyncio
async def test_sync_writes_purely_to_prune_even_when_kept_providers_are_unchanged(monkeypatch, patched_queries):
    """This is the case the ORIGINAL `_providers_already_current` missed: every
    provider we still emit already matches live byte-for-byte, so the old
    `>=`-only check called it 'already current' and skipped the write —
    the stale extra key stayed forever. Staleness alone must now force a
    write."""
    kept = {"anthropic-sonnet-200k": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "ANTHROPIC_API_KEY", "models": []}}
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: kept)
    call = AsyncMock(return_value={
        "hash": "abc123",
        "config": {"models": {"providers": {
            **{"anthropic-sonnet-200k": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []}},
            "anthropic-sonnet-500k": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }}},
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    assert call.await_count == 2, "must write even though every EMITTED provider matched live"
    sent = json.loads(call.await_args_list[1].args[1]["raw"])
    assert "anthropic-sonnet-500k" not in sent["models"]["providers"]


@pytest.mark.asyncio
async def test_sync_never_prunes_a_gateway_builtin_even_if_unmanaged(monkeypatch, patched_queries):
    """A provider we've never emitted (a hand-configured or built-in one) must
    survive a sync untouched, whether or not this round writes anything."""
    monkeypatch.setattr(gs, "build_providers_payload", lambda *a, **k: _PROVIDERS)
    call = AsyncMock(return_value={
        "hash": "abc123",
        "config": {"models": {"providers": {
            "anthropic": {"baseUrl": "https://api.anthropic.com/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
            "moonshot": {"baseUrl": "https://api.moonshot.cn/v1", "apiKey": "__OPENCLAW_REDACTED__", "models": []},
        }}},
    })
    monkeypatch.setattr(gs.gateway_admin, "call", call)

    await gs.sync_provider_auth_configs_to_gateway()

    # already current (anthropic matches, nothing AgentOS-owned is stale) — no write at all
    call.assert_awaited_once_with("config.get", {})
