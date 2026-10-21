"""Scoped and revocable API key service for AgentOS external API.

Enforces:
- Constant-time token verification (hmac.compare_digest)
- SHA-256 storage hashing
- Prefix retention for key identification
- Revocation timestamp checks
- Scopes gating
- Principal injection (ApiPrincipal)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
import uuid

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field

from app.database import get_pool

log = logging.getLogger(__name__)

API_KEY_PREFIX = "agos_live_sk_"


class ApiPrincipal(BaseModel):
    """The principal authenticated via an API key."""

    key_id: str
    owner_uid: str
    actor_id: str
    key_prefix: str
    scopes: list[str] = Field(default_factory=list)
    name: str = ""

    @property
    def uid(self) -> str:
        return self.owner_uid

    def has_scope(self, scope: str) -> bool:
        if "*" in self.scopes or "admin" in self.scopes:
            return True
        return scope in self.scopes

    def __getitem__(self, item: str) -> Any:
        if item == "uid":
            return self.owner_uid
        if item == "actor_id":
            return self.actor_id
        if item == "name":
            return self.name
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except (KeyError, AttributeError):
            return default


def generate_api_key_token() -> str:
    """Generate a high-entropy secret token with canonical prefix."""
    entropy = secrets.token_urlsafe(32)
    return f"{API_KEY_PREFIX}{entropy}"


def generate_api_key() -> tuple[str, str, str]:
    """Generate raw token, prefix (first 16 chars), and sha256 hash."""
    token = generate_api_key_token()
    return token, token[:16], hash_api_key(token)


def hash_api_key(token: str) -> str:
    """Compute canonical SHA-256 hex digest for storage/lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_api_key(token: str, expected_hash: str) -> bool:
    """Verify raw token against expected SHA-256 hash in constant time."""
    if not token.startswith(API_KEY_PREFIX):
        return False
    actual_hash = hash_api_key(token)
    return hmac.compare_digest(actual_hash, expected_hash)


def has_scopes(user_scopes: Sequence[str], required_scopes: Sequence[str]) -> bool:
    """Check whether user_scopes satisfies required_scopes (supports * and prefix:*)."""
    if not required_scopes:
        return True
    user_set = set(user_scopes)
    if "*" in user_set or "admin" in user_set:
        return True
    for req in required_scopes:
        matched = False
        for s in user_set:
            if s == req:
                matched = True
                break
            if s.endswith(":*") and req.startswith(s[:-1]):
                matched = True
                break
        if not matched:
            return False
    return True


async def create_api_key(
    pool_or_conn,
    owner_uid: str,
    actor_id: str | uuid.UUID | None = None,
    name: str = "",
    scopes: Sequence[str] | None = None,
    expires_at: Optional[datetime] = None,
) -> tuple[str, dict]:
    """Create a new API key record. Returns (raw_token, record)."""
    raw_token = generate_api_key_token()
    key_hash = hash_api_key(raw_token)
    key_prefix = raw_token[:16]
    key_scopes = list(scopes) if scopes is not None else []
    key_id = uuid.uuid4()
    actor_uuid = uuid.UUID(str(actor_id)) if actor_id is not None else uuid.uuid4()

    await pool_or_conn.execute(
        """
        INSERT INTO public.api_keys (
            id, key_hash, key_prefix, owner_uid, actor_id, name, scopes, expires_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        key_id,
        key_hash,
        key_prefix,
        owner_uid,
        actor_uuid,
        name,
        key_scopes,
        expires_at,
    )

    record = {
        "id": str(key_id),
        "key_prefix": key_prefix,
        "owner_uid": owner_uid,
        "actor_id": str(actor_uuid),
        "name": name,
        "scopes": key_scopes,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "revoked_at": None,
    }
    return raw_token, record


async def revoke_api_key(
    pool_or_conn,
    key_id: str | uuid.UUID,
    owner_uid: Optional[str] = None,
) -> bool:
    """Revoke an API key by marking revoked_at = now()."""
    key_uuid = uuid.UUID(str(key_id))
    if owner_uid:
        tag = await pool_or_conn.execute(
            """
            UPDATE public.api_keys
            SET revoked_at = now()
            WHERE id = $1 AND owner_uid = $2 AND revoked_at IS NULL
            """,
            key_uuid,
            owner_uid,
        )
    else:
        tag = await pool_or_conn.execute(
            """
            UPDATE public.api_keys
            SET revoked_at = now()
            WHERE id = $1 AND revoked_at IS NULL
            """,
            key_uuid,
        )
    return tag.endswith(" 1")


async def authenticate_api_key(
    token: str,
    required_scopes: Sequence[str] | None = None,
    pool=None,
) -> ApiPrincipal:
    """Validate raw token against public.api_keys and return ApiPrincipal."""
    if not token.startswith(API_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format",
        )

    token_hash = hash_api_key(token)
    if pool is None:
        pool = get_pool()

    row = await pool.fetchrow(
        "SELECT * FROM public.api_keys WHERE key_hash = $1",
        token_hash,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Constant time verification of hash
    if not hmac.compare_digest(row["key_hash"], token_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    if row["revoked_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has been revoked",
        )

    if row["expires_at"] is not None and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has expired",
        )

    scopes = list(row["scopes"] or [])
    if required_scopes:
        if not has_scopes(scopes, required_scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: API key missing required scope(s): {', '.join(required_scopes)}",
            )

    try:
        await pool.execute(
            "UPDATE public.api_keys SET last_used_at = now() WHERE id = $1",
            row["id"],
        )
    except Exception as exc:
        log.warning("Failed to update last_used_at for key %s: %s", row["id"], exc)

    return ApiPrincipal(
        key_id=str(row["id"]),
        owner_uid=row["owner_uid"],
        actor_id=str(row["actor_id"]),
        key_prefix=row["key_prefix"],
        scopes=scopes,
        name=row.get("name") or "",
    )


async def _extract_principal(
    request: Request,
    scopes: Sequence[str] | None = None,
) -> ApiPrincipal:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = auth_header.removeprefix("Bearer ").strip()
    return await authenticate_api_key(token, scopes)


def require_api_key(
    scopes_or_request: Sequence[str] | str | Request | None = None,
):
    """FastAPI dependency factory or dependency function.

    Usage:
      - `principal: ApiPrincipal = Depends(require_api_key)`
      - `principal: ApiPrincipal = Depends(require_api_key(["goals:create"]))`
      - `principal: ApiPrincipal = Depends(require_api_key("goals:read"))`
    """
    if isinstance(scopes_or_request, Request):
        return _extract_principal(scopes_or_request, scopes=None)

    expected_scopes = (
        [scopes_or_request]
        if isinstance(scopes_or_request, str)
        else list(scopes_or_request or [])
    )

    async def _dependency(request: Request) -> ApiPrincipal:
        return await _extract_principal(request, scopes=expected_scopes)

    return _dependency
