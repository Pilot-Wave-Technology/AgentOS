"""Unit and integration tests for API key management and authentication.

Tests cover:
  1. Token generation: prefix, length, hash calculation, prefix extraction.
  2. Constant-time verification.
  3. Scope matching (exact, wildcards like *, goals:*).
  4. Database operations: create, authenticate, update last_used_at, revoke.
  5. FastAPI dependency `require_api_key`:
     - missing Authorization header -> 401
     - non-Bearer authorization -> 401
     - malformed token prefix -> 401
     - invalid/unknown token -> 401
     - revoked token -> 401
     - missing required scope -> 403
     - valid token with matching scope -> 200 and injects ApiPrincipal
  6. Migration syntax and verification.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any
from unittest.mock import AsyncMock
import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from app.services.api_keys import (
    API_KEY_PREFIX as KEY_PREFIX,
    ApiPrincipal,
    authenticate_api_key,
    create_api_key,
    generate_api_key,
    hash_api_key,
    has_scopes,
    require_api_key,
    revoke_api_key,
    verify_api_key,
)
from app.services.tests.shipped_tree import (
    IS_DISTRIBUTION,
    baseline_mentions,
    create_table,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = _REPO_ROOT / "migrations" / "api_keys.sql"


# ── Mock Database Connection ──────────────────────────────────────────────────

class MockRecord(dict):
    """Emulates asyncpg.Record."""
    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


class InMemoryApiKeysConn:
    def __init__(self):
        self.rows: dict[uuid.UUID, dict[str, Any]] = {}
        self.executed_queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> MockRecord | None:
        norm = " ".join(query.strip().split())
        if "WHERE key_hash = $1" in norm:
            key_hash_arg = args[0]
            for row in self.rows.values():
                if row["key_hash"] == key_hash_arg:
                    return MockRecord(row)
            return None
        return None

    async def execute(self, query: str, *args: Any) -> str:
        norm = " ".join(query.strip().split())
        self.executed_queries.append(norm)

        if "INSERT INTO public.api_keys" in norm:
            key_id = args[0]
            key_hash = args[1]
            key_prefix = args[2]
            owner_uid = args[3]
            actor_id = args[4]
            name = args[5]
            scopes = args[6]
            expires_at = args[7]
            row = {
                "id": key_id,
                "key_hash": key_hash,
                "key_prefix": key_prefix,
                "owner_uid": owner_uid,
                "actor_id": actor_id,
                "name": name,
                "scopes": scopes,
                "created_at": datetime.now(timezone.utc),
                "last_used_at": None,
                "expires_at": expires_at,
                "revoked_at": None,
            }
            self.rows[key_id] = row
            return "INSERT 0 1"

        if "UPDATE public.api_keys SET last_used_at" in norm:
            key_id_arg = args[0]
            for row in self.rows.values():
                if row["id"] == key_id_arg or str(row["id"]) == str(key_id_arg):
                    row["last_used_at"] = datetime.now(timezone.utc)
                    return "UPDATE 1"
            return "UPDATE 0"

        if "UPDATE public.api_keys SET revoked_at" in norm:
            key_id_arg = args[0]
            owner_arg = args[1] if len(args) > 1 else None
            for row in self.rows.values():
                if row["id"] == key_id_arg or str(row["id"]) == str(key_id_arg):
                    if owner_arg is None or row["owner_uid"] == owner_arg:
                        row["revoked_at"] = datetime.now(timezone.utc)
                        return "UPDATE 1"
            return "UPDATE 0"

        return "OK"


# ── Unit Tests: Token Generation & Verification ──────────────────────────────

def test_generate_api_key():
    token, prefix, key_hash = generate_api_key()
    assert token.startswith(KEY_PREFIX)
    assert len(token) > len(KEY_PREFIX) + 20
    assert prefix == token[:16]
    assert key_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert verify_api_key(token, key_hash) is True
    assert verify_api_key("wrong_token", key_hash) is False


def test_hash_api_key():
    token = "agos_live_sk_sample123456789"
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert hash_api_key(token) == expected


def test_has_scopes():
    # Wildcard superuser
    assert has_scopes(["*"], ["goals:create"]) is True
    assert has_scopes(["*"], ["actions:read", "questions:write"]) is True

    # Namespace wildcard
    assert has_scopes(["goals:*"], ["goals:create"]) is True
    assert has_scopes(["goals:*"], ["goals:read"]) is True
    assert has_scopes(["goals:*"], ["actions:read"]) is False

    # Exact matches
    assert has_scopes(["goals:create", "goals:read"], ["goals:create"]) is True
    assert has_scopes(["goals:create"], ["goals:create", "goals:read"]) is False

    # Empty requirements
    assert has_scopes([], []) is True
    assert has_scopes(["anything"], []) is True


# ── Integration Tests: Key Lifecycle ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_and_authenticate_key():
    conn = InMemoryApiKeysConn()
    token, rec = await create_api_key(
        pool_or_conn=conn,
        owner_uid="user-123",
        name="MARL Orchestrator",
        scopes=["goals:create", "questions:*"],
    )

    key_id = rec["id"]
    assert token.startswith(KEY_PREFIX)
    assert isinstance(key_id, str)

    # Authenticate valid token
    principal = await authenticate_api_key(token, pool=conn)
    assert principal.key_id == key_id
    assert principal.name == "MARL Orchestrator"
    assert principal.owner_uid == "user-123"
    assert "goals:create" in principal.scopes
    assert "questions:*" in principal.scopes

    # Revoke key
    revoked = await revoke_api_key(conn, key_id, owner_uid="user-123")
    assert revoked is True

    # Authenticate after revocation should raise 401
    with pytest.raises(Exception) as exc_info:
        await authenticate_api_key(token, pool=conn)
    assert "401" in str(exc_info.value) or exc_info.value.status_code == 401


# ── Integration Tests: FastAPI Dependency require_api_key ─────────────────────

def test_fastapi_require_api_key_dependency():
    app = FastAPI()
    conn = InMemoryApiKeysConn()

    # Pre-create two keys: one full access, one scoped
    import asyncio
    token_admin, _ = asyncio.run(
        create_api_key(conn, owner_uid="admin-1", name="Admin", scopes=["*"])
    )
    token_limited, _ = asyncio.run(
        create_api_key(conn, owner_uid="user-2", name="Limited", scopes=["goals:read"])
    )

    @app.get("/test-goals-create")
    async def create_goal_endpoint(
        principal: ApiPrincipal = Depends(require_api_key(["goals:create"])),
    ):
        return {"status": "ok", "user": principal.owner_uid}

    @app.get("/test-open")
    async def open_endpoint(
        principal: ApiPrincipal = Depends(require_api_key()),
    ):
        return {"status": "ok", "key_name": principal.name}

    # Override get_pool or conn in require_api_key
    from app.services import api_keys as api_keys_mod
    class DummyPool:
        async def fetchrow(self, *args, **kwargs):
            return await conn.fetchrow(*args, **kwargs)

        async def execute(self, *args, **kwargs):
            return await conn.execute(*args, **kwargs)

        def acquire(self):
            class AcquireContext:
                async def __aenter__(self):
                    return conn
                async def __aexit__(self, *args):
                    pass
            return AcquireContext()

    dummy_pool = DummyPool()
    import unittest.mock
    with unittest.mock.patch.object(api_keys_mod, "get_pool", return_value=dummy_pool):
        client = TestClient(app)

        # 1. Missing header -> 401
        res = client.get("/test-goals-create")
        assert res.status_code == 401

        # 2. Non-Bearer auth -> 401
        res = client.get("/test-goals-create", headers={"Authorization": "Basic 12345"})
        assert res.status_code == 401

        # 3. Invalid token prefix -> 401
        res = client.get("/test-goals-create", headers={"Authorization": "Bearer not_an_agos_key"})
        assert res.status_code == 401

        # 4. Unknown token -> 401
        res = client.get("/test-goals-create", headers={"Authorization": f"Bearer {KEY_PREFIX}randomletters12345"})
        assert res.status_code == 401

        # 5. Limited token lacking goals:create -> 403
        res = client.get("/test-goals-create", headers={"Authorization": f"Bearer {token_limited}"})
        assert res.status_code == 403
        assert "missing required scope" in res.json()["detail"].lower()

        # 6. Admin token with wildcard scope -> 200
        res = client.get("/test-goals-create", headers={"Authorization": f"Bearer {token_admin}"})
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "user": "admin-1"}

        # 7. Limited token on open endpoint -> 200
        res = client.get("/test-open", headers={"Authorization": f"Bearer {token_limited}"})
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "key_name": "Limited"}


# ── Migration DDL Verification ────────────────────────────────────────────────

def test_migration_ddl_exists_and_contains_schema():
    if IS_DISTRIBUTION:
        body = create_table("api_keys")
        for col, decl in (("key_hash", "text NOT NULL"),
                          ("key_prefix", "text NOT NULL"),
                          ("scopes", "text[] DEFAULT '{}'::text[] NOT NULL"),
                          ("revoked_at", "timestamp with time zone")):
            assert re.search(rf"^\s+{col} {re.escape(decl)}", body, re.M), col
        assert baseline_mentions(r"ADD CONSTRAINT \w+ UNIQUE \(key_hash\)")
        assert baseline_mentions(r"CREATE INDEX idx_api_keys_hash ON public\.api_keys")
        return
    assert _MIGRATION_PATH.is_file(), f"Migration file missing at {_MIGRATION_PATH}"
    ddl = " ".join(_MIGRATION_PATH.read_text(encoding="utf-8").upper().split())
    assert "CREATE TABLE IF NOT EXISTS PUBLIC.API_KEYS" in ddl
    assert "KEY_HASH TEXT NOT NULL UNIQUE" in ddl
    assert "KEY_PREFIX TEXT NOT NULL" in ddl
    assert "SCOPES TEXT[] NOT NULL" in ddl
    assert "REVOKED_AT TIMESTAMPTZ" in ddl
    assert "IDX_API_KEYS_HASH" in ddl
