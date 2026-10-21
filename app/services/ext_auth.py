"""Local Execution Mode — extension sign-in via one-time code (option B, §6).

The control plane is the credential authority. After the human signs in
(Firebase, existing web flow), the CP hands the extension a short-lived, single-
use CODE via a loopback redirect. The extension exchanges it for a short-lived
ext-session token + a long-lived refresh handle — so NO long-lived Firebase
credential ever lands on the user's machine. See LOCAL_EXECUTION_MODE_PLAN.md §6.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.database import get_pool
from app.internal_auth import mint_ext_session_token

_log = logging.getLogger(__name__)

_CODE_TTL_SECONDS = 120
_SESSION_TTL_SECONDS = 3600


async def issue_code(uid: str) -> str:
    """Mint a single-use, short-TTL code for the loopback redirect."""
    code = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=_CODE_TTL_SECONDS)
    await get_pool().execute(
        "INSERT INTO ext_auth_codes (code, uid, expires_at) VALUES ($1, $2, $3)",
        code, uid, expires,
    )
    _log.info(
        "issue_code: minted uid=%s issued_at=%s expires_at=%s ttl_s=%s",
        uid, now.isoformat(), expires.isoformat(), _CODE_TTL_SECONDS,
    )
    return code


async def exchange_code(code: str) -> dict:
    """One-time exchange: consume the code, create a refresh handle, mint a session
    token. Returns {ok, uid, session_token, session_expires_in, refresh_handle}."""
    pool = get_pool()
    row = await pool.fetchrow(
        """UPDATE ext_auth_codes SET used_at = now()
            WHERE code = $1 AND used_at IS NULL AND expires_at > now()
         RETURNING uid""",
        code,
    )
    if not row:
        # Distinguish "never existed / already used" from "expired but present"
        # so a slow extension<->loopback roundtrip (cold start, network) is
        # visible in logs instead of looking identical to a bogus code.
        stale = await pool.fetchrow(
            "SELECT uid, used_at, expires_at FROM ext_auth_codes WHERE code = $1", code,
        )
        if stale is None:
            _log.warning("exchange_code: rejected — code not found")
        elif stale["used_at"] is not None:
            _log.warning(
                "exchange_code: rejected — code already used uid=%s used_at=%s",
                stale["uid"], stale["used_at"].isoformat(),
            )
        else:
            _log.warning(
                "exchange_code: rejected — code expired uid=%s expires_at=%s now=%s",
                stale["uid"], stale["expires_at"].isoformat(),
                datetime.now(timezone.utc).isoformat(),
            )
        return {"ok": False, "reason": "invalid_or_expired_code"}
    uid = row["uid"]
    handle = secrets.token_urlsafe(32)
    await pool.execute("INSERT INTO ext_sessions (handle, uid) VALUES ($1, $2)", handle, uid)
    _log.info("exchange_code: succeeded uid=%s", uid)
    return {
        "ok": True,
        "uid": uid,
        "session_token": mint_ext_session_token(uid, _SESSION_TTL_SECONDS),
        "session_expires_in": _SESSION_TTL_SECONDS,
        "refresh_handle": handle,
    }


async def refresh_session(handle: str) -> dict:
    """Exchange a live (non-revoked) refresh handle for a fresh session token."""
    row = await get_pool().fetchrow(
        """UPDATE ext_sessions SET last_used_at = now()
            WHERE handle = $1 AND revoked_at IS NULL
         RETURNING uid""",
        handle,
    )
    if not row:
        _log.warning("refresh_session: rejected — handle invalid or revoked")
        return {"ok": False, "reason": "invalid_or_revoked_handle"}
    _log.info("refresh_session: succeeded uid=%s", row["uid"])
    return {
        "ok": True,
        "session_token": mint_ext_session_token(row["uid"], _SESSION_TTL_SECONDS),
        "session_expires_in": _SESSION_TTL_SECONDS,
    }


async def revoke_handle(handle: str) -> None:
    """Sign-out: revoke a refresh handle so it can't mint further session tokens."""
    result = await get_pool().execute(
        "UPDATE ext_sessions SET revoked_at = now() WHERE handle = $1 AND revoked_at IS NULL",
        handle,
    )
    _log.info("revoke_handle: %s", result)
