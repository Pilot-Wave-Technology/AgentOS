"""Fernet encryption/decryption for tool credentials.

Credentials are encrypted in-process before DB storage. The key comes from
CREDENTIAL_ENCRYPTION_KEY env var, loaded at startup alongside DATABASE_URL.
An in-memory cache (5-minute TTL) avoids redundant decryption on hot paths.
"""
import logging
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

log = logging.getLogger(__name__)

_cache: dict[str, tuple[str, float]] = {}  # credential_id → (plaintext, expires_at)
_CACHE_TTL = 300  # 5 minutes


def _fernet() -> Fernet:
    key = get_settings().credential_encryption_key
    if not key:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set — "
            "see documentation/deployment/CREDENTIAL_ENCRYPTION_KEY.md"
        )
    return Fernet(key.encode())


def encrypt_value(plaintext: str) -> str:
    """Encrypt a credential value. Returns a Fernet token (base64url TEXT)."""
    token = _fernet().encrypt(plaintext.encode()).decode()
    log.debug("credential encrypted")
    return token


def decrypt_value(credential_id: str, encrypted_value: str) -> str:
    """Decrypt a credential, using in-memory cache to avoid repeated work."""
    now = time.time()
    cached = _cache.get(credential_id)
    if cached:
        value, expires_at = cached
        if now < expires_at:
            return value

    try:
        value = _fernet().decrypt(encrypted_value.encode()).decode()
    except InvalidToken as exc:
        log.error("credential decryption failed for %s: %s", credential_id, exc)
        raise ValueError(f"credential {credential_id}: decryption failed") from exc

    _cache[credential_id] = (value, now + _CACHE_TTL)
    log.debug("credential %s decrypted and cached", credential_id)
    return value


def evict_cache(credential_id: str) -> None:
    """Evict a credential from the in-memory cache (call after rotation)."""
    _cache.pop(credential_id, None)
    log.debug("credential %s evicted from cache", credential_id)


def inject_auth_header(
    headers: dict[str, str],
    credential_type: str,
    plaintext: str,
    header_name: Optional[str] = None,
) -> None:
    """Mutate headers dict in-place to add the appropriate auth header."""
    if credential_type == "bearer":
        headers["Authorization"] = f"Bearer {plaintext}"
    elif credential_type == "api_key":
        key = header_name or "X-API-Key"
        headers[key] = plaintext
    elif credential_type == "basic":
        import base64
        encoded = base64.b64encode(plaintext.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
