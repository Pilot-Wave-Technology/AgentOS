"""Unit tests for `GET /internal/model_catalog` and Phase-A2 removals.

Two axes are covered here:

  1. Endpoint behaviour — auth (401 on missing / wrong Bearer, 200 on
     correct Bearer), response envelope shape (`{models, providers,
     generated_at}` with ISO-8601 UTC timestamp), correct forwarding of
     the emitter payload verbatim, empty-table edge case, and the
     500 `{error: ...}` DB-failure path.
  2. Push-mechanism removal — imports of the deleted modules fail,
     the sync endpoint is not registered on the router, the two
     `main.py` startup helpers are gone, and `GatewayAdmin.set_config`
     no longer exists. Locks the Phase-A2 removal in place.

No live Postgres, no live gateway. The router's DB fetch functions are
monkey-patched on the router module itself; the auth check runs through
the real `verify_internal_bearer` so the Bearer path is exercised
end-to-end.
"""
from __future__ import annotations

import importlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixtures.
# ---------------------------------------------------------------------------


# `OPENCLAW_AUTH_TOKEN` is set to "test-token" by conftest.py; the router
# calls `verify_internal_bearer` which compares against that value.
_VALID_BEARER = {"Authorization": "Bearer test-token"}
_ISO_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$"
)


@pytest.fixture
def router_mod(monkeypatch):
    """Import the router fresh, stub the DB fetches, return the module."""
    monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", "test-token")
    from app.routers import model_registry as mod

    async def _empty_base_models(_pool):
        return []

    async def _empty_native_providers(_pool):
        return []

    monkeypatch.setattr(mod, "fetch_active_base_models", _empty_base_models)
    monkeypatch.setattr(mod, "fetch_native_provider_configs", _empty_native_providers)
    # `get_pool()` is only used as an opaque handle here — the query stub
    # ignores its argument. Return a sentinel so a real pool is never
    # required (calling `get_pool()` before `init_pool()` raises).
    monkeypatch.setattr(mod, "get_pool", lambda: object())
    return mod


def _client(mod) -> TestClient:
    """Fresh FastAPI app with only the catalog router mounted."""
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Endpoint — auth.
# ---------------------------------------------------------------------------


def test_missing_authorization_returns_401(router_mod):
    resp = _client(router_mod).get("/internal/model_catalog")
    assert resp.status_code == 401


def test_wrong_bearer_returns_401(router_mod):
    resp = _client(router_mod).get(
        "/internal/model_catalog",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_bearer_without_scheme_returns_401(router_mod):
    """Sending just the token (no `Bearer ` prefix) must fail."""
    resp = _client(router_mod).get(
        "/internal/model_catalog",
        headers={"Authorization": "test-token"},
    )
    assert resp.status_code == 401


def test_correct_bearer_returns_200(router_mod):
    resp = _client(router_mod).get("/internal/model_catalog", headers=_VALID_BEARER)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 1. Endpoint — envelope shape.
# ---------------------------------------------------------------------------


def test_empty_tables_returns_empty_dicts_with_timestamp(router_mod):
    """No models + no providers still produces the full envelope."""
    resp = _client(router_mod).get("/internal/model_catalog", headers=_VALID_BEARER)
    body = resp.json()

    assert set(body.keys()) == {"models", "providers", "providerCredentials", "generated_at"}
    assert body["models"] == {}
    assert body["providers"] == {}
    assert body["providerCredentials"] == {}
    assert isinstance(body["generated_at"], str)
    assert _ISO_UTC_RE.match(body["generated_at"]), (
        f"generated_at not ISO-8601 UTC: {body['generated_at']!r}"
    )


_GOOGLE_NATIVE = {
    "id": "google", "base_url": "https://generativelanguage.googleapis.com/v1beta",
    "api_key_source": "GEMINI_API_KEY", "api_type": "google-generative-ai",
    "secret_project": "test-project", "secret_id": "gemini-api-key", "secret_version": "latest",
}


def test_envelope_contains_emitter_payload_verbatim(monkeypatch):
    """Response `models`/`providers` are exactly what the emitter built."""
    from app.routers import model_registry as mod

    async def _base_models(_pool):
        return [
            {"id": "google/gemini-2.5-flash",
             "label": "Gemini 2.5 Flash",
             "context_window": 1_000_000,
             "provider_shorthand": "flash",
             "available_context_windows": [100_000],
             "tags": []},
        ]

    async def _native_providers(_pool):
        return [_GOOGLE_NATIVE]

    monkeypatch.setattr(mod, "fetch_active_base_models", _base_models)
    monkeypatch.setattr(mod, "fetch_native_provider_configs", _native_providers)
    monkeypatch.setattr(mod, "get_pool", lambda: object())

    resp = _client(mod).get("/internal/model_catalog", headers=_VALID_BEARER)
    body = resp.json()

    # models: exact five-field envelope per id, no extra columns — the
    # base entry PLUS its one synthesized context-window variant.
    assert set(body["models"].keys()) == {
        "google/gemini-2.5-flash",
        "google-flash-100k/gemini-2.5-flash-100k",
    }
    for mid, entry in body["models"].items():
        assert set(entry.keys()) == {"id", "name", "contextWindow", "reasoning", "input"}
    assert body["models"]["google/gemini-2.5-flash"] == {
        "id": "google/gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "contextWindow": 1_000_000,
        "reasoning": False,
        "input": ["text"],
    }
    # providers: full ProviderConfig shape, baseUrl/apiKey/api from the
    # native provider row, one nested model entry for the variant, plus
    # the base's own native (bare-id) entry (see build_providers_payload).
    assert body["providers"] == {
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
                "input": ["text"],
            }],
        },
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
                "input": ["text"],
            }],
        },
    }


def test_envelope_never_leaks_extra_row_columns(monkeypatch):
    """A row with unknown columns must not leak into the emitted body.

    Guards against a future dict(row) refactor of the emitter — mirrors
    the same guard test_emitter.py enforces.
    """
    from app.routers import model_registry as mod

    async def _base_models(_pool):
        return [{
            "id": "google/gemini-2.5-flash",
            "label": "Gemini 2.5 Flash",
            "context_window": 1_000_000,
            "provider_shorthand": None,
            "available_context_windows": [],
            # Poison columns — must NOT appear in the response body.
            "internal_secret": "leak_me",
            "provider_id": "google",
            "tier": "fast",
            "status": "active",
        }]

    async def _empty_native_providers(_pool):
        return []

    monkeypatch.setattr(mod, "fetch_active_base_models", _base_models)
    monkeypatch.setattr(mod, "fetch_native_provider_configs", _empty_native_providers)
    monkeypatch.setattr(mod, "get_pool", lambda: object())

    resp = _client(mod).get("/internal/model_catalog", headers=_VALID_BEARER)
    assert resp.status_code == 200
    raw = resp.text
    for poison in ("internal_secret", "leak_me"):
        assert poison not in raw, f"leaked {poison!r}: {raw}"


def test_content_type_is_json(router_mod):
    resp = _client(router_mod).get(
        "/internal/model_catalog", headers=_VALID_BEARER,
    )
    assert resp.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# 1. Endpoint — failure surface.
# ---------------------------------------------------------------------------


def test_db_fetch_failure_returns_500_error_body(monkeypatch):
    """DB error surfaces as 500 {error: ...}, does not raise."""
    from app.routers import model_registry as mod

    async def _boom(_pool):
        raise ConnectionError("db down")

    monkeypatch.setattr(mod, "fetch_active_base_models", _boom)
    monkeypatch.setattr(mod, "get_pool", lambda: object())

    resp = _client(mod).get("/internal/model_catalog", headers=_VALID_BEARER)
    assert resp.status_code == 500
    body = resp.json()
    assert "db down" in body.get("error", "")


# ---------------------------------------------------------------------------
# 2. Removal / deprecation of the Phase A1 push pipeline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "modpath",
    [
        "app.services.model_registry.sync",
        "app.services.model_registry.listener",
    ],
)
def test_deleted_service_modules_no_longer_importable(modpath):
    """`sync.py` and `listener.py` were removed — import must fail."""
    with pytest.raises(ImportError):
        importlib.import_module(modpath)


@pytest.mark.parametrize(
    "modpath",
    [
        "app.services.model_registry.tests.test_sync",
        "app.services.model_registry.tests.test_listener",
        "app.services.model_registry.tests.test_router",
    ],
)
def test_deleted_test_modules_no_longer_importable(modpath):
    with pytest.raises(ImportError):
        importlib.import_module(modpath)


def test_service_package_does_not_re_export_push_symbols():
    """The package `__init__.py` must no longer re-export the push API."""
    import app.services.model_registry as pkg

    for removed in ("sync_to_gateway", "run_listener", "SyncResult"):
        assert not hasattr(pkg, removed), (
            f"stale re-export: {removed!r} should be gone"
        )


def test_service_package_no_longer_re_exports_v1_management_surface():
    """The v1 CRUD re-exports (`create_model`, `list_models`, etc. — the
    frozen family-A surface) were removed 2026-08-06 after confirming
    zero live callers anywhere, including the package root itself
    (every real caller already went through `management`/`management`'s
    `*_v2` functions directly). `__init__.py` is docstring-only now."""
    import app.services.model_registry as pkg

    removed_v1_surface = {
        "ManagementError", "ModelValidationError", "ProviderValidationError",
        "list_models", "get_model", "create_model", "update_model",
        "set_model_status",
        "list_providers", "get_provider", "create_provider",
        "update_provider", "set_provider_status",
    }
    present = removed_v1_surface & set(dir(pkg))
    assert not present, f"stale v1 re-export(s) still present: {present}"


def test_gateway_admin_set_config_method_is_removed():
    """`GatewayAdmin.set_config` was the only push-side entry point."""
    from app.openclaw import GatewayAdmin

    assert not hasattr(GatewayAdmin, "set_config"), (
        "GatewayAdmin.set_config was removed by Phase A2 — see "
        "documentation/MODEL_REGISTRY_CONTRACT.md"
    )


def test_main_module_no_longer_defines_push_startup_helpers():
    """The two `main.py` startup helpers + their scheduling calls were deleted.

    Asserted at the source level rather than by importing `app.main` — the
    latter drags in every router in the app (including optional heavy deps
    like `google.genai`) which is unnecessary for a removal check.
    """
    from pathlib import Path
    import ast, inspect
    import app as app_pkg

    main_path = Path(inspect.getfile(app_pkg)).parent / "main.py"
    src = main_path.read_text()

    # No leftover reference to either helper anywhere in the file (def,
    # call site, or comment referring to them as live code).
    for removed in ("_model_registry_initial_sync", "_model_registry_listener"):
        assert removed not in src, (
            f"stale startup helper name {removed!r} found in app/main.py"
        )

    # AST-level: no top-level function definition with either name and
    # no async coroutine definition either.
    tree = ast.parse(src)
    defined_names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_model_registry_initial_sync" not in defined_names
    assert "_model_registry_listener" not in defined_names

    # And the module no longer imports the removed service symbols.
    for banned in ("sync_to_gateway", "run_listener", "SyncResult"):
        assert banned not in src, (
            f"stale import/reference in app/main.py: {banned!r}"
        )


def test_router_does_not_expose_sync_endpoint(router_mod):
    """`POST /api/gateway/models/sync` was deleted — only the pull GET remains."""
    paths_methods = set()
    for r in router_mod.router.routes:
        for m in getattr(r, "methods", ()) or ():
            paths_methods.add((m, getattr(r, "path", None)))
    assert ("GET", "/internal/model_catalog") in paths_methods
    assert ("POST", "/api/gateway/models/sync") not in paths_methods


def test_router_module_does_not_import_removed_symbols():
    """A regression check on the router's import graph.

    `gateway_admin` is deliberately NOT banned here (unlike the other
    v1-era symbols) — every catalog-affecting admin write now calls
    `_notify_gateway_reload()` -> `gateway_admin.call("models.reload", ...)`
    so a newly added/edited model reaches the gateway immediately instead
    of waiting up to 5 minutes for the plugin's self-heal poll.
    """
    import inspect
    from app.routers import model_registry as mod

    src = inspect.getsource(mod)
    for banned in ("sync_to_gateway", "run_listener", "SyncResult", "set_config"):
        assert banned not in src, (
            f"router should not reference removed symbol {banned!r}"
        )
