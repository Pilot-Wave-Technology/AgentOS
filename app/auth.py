"""Firebase JWT verification + user helpers.

Firebase Admin SDK handles JWT verification only.
All user/membership data lives in PostgreSQL.
"""
import json
import logging
import uuid
from typing import Optional

import firebase_admin
from firebase_admin import auth as fb_auth, credentials
from fastapi import Request, HTTPException, status

from app.config import get_settings
from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import USER_CREATED
from app.roles import SystemRole, GoalRole

_log = logging.getLogger(__name__)

# Canonical system actor (…0001), registered in the `actors` table. Re-exported
# here for the modules that historically imported it from app.auth. Unified from
# the old, unregistered …0000 pre-user value — see app/ledger/actors.py and
# LEDGER_PROVENANCE_REVIEW.md.
from app.ledger.actors import SYSTEM_ACTOR_ID  # noqa: F401,E402


# ── Signup gate exceptions ───────────────────────────────────────────────────

class SignupRequired(Exception):
    """Firebase user exists but has no signup request — needs to sign up."""

class SignupPending(Exception):
    """Signup request submitted but not yet reviewed."""

class SignupRejected(Exception):
    """Signup request was rejected by an admin."""


# ── Firebase helpers ─────────────────────────────────────────────────────────

def _ensure_firebase_app():
    settings = get_settings()
    if not firebase_admin._apps:
        sa_json = settings.firebase_service_account_json
        if sa_json:
            try:
                sa_dict = json.loads(sa_json)
                cred = credentials.Certificate(sa_dict)
            except (json.JSONDecodeError, ValueError):
                cred = credentials.Certificate(sa_json)
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id})


def verify_firebase_token(token: str) -> dict:
    """Verify a Firebase ID token and return its decoded claims."""
    _ensure_firebase_app()
    try:
        decoded = fb_auth.verify_id_token(token)
        return decoded
    except Exception as exc:
        _log.warning("auth: Firebase token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired Firebase token: {exc}",
        )


def _get_token_from_request(request: Request) -> Optional[str]:
    # Cookie takes priority: the proxy forwards an OIDC service-account token in
    # the Authorization header so Cloud Run accepts the call, but that token is
    # NOT a Firebase JWT. Read the cookie first so management-service routes
    # authenticate against the user's firebase_token, not the invoker token.
    cookie_token = request.cookies.get("firebase_token")
    if cookie_token:
        _log.debug("auth: using firebase_token cookie for path=%s", request.url.path)
        return cookie_token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        _log.debug("auth: no cookie, falling back to Authorization header for path=%s", request.url.path)
        return auth_header.removeprefix("Bearer ").strip()
    _log.debug("auth: no token found (no cookie, no Bearer header) for path=%s", request.url.path)
    return None


# ── Main user dependency ─────────────────────────────────────────────────────

async def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency — resolves the Firebase JWT to a full user dict
    (firebase claims + PostgreSQL profile including system_role).

    Signup gate:
      - No user row + no signup_request → raise SignupRequired
      - signup_request pending → raise SignupPending
      - signup_request rejected → raise SignupRejected
      - signup_request approved → auto-create user row (one-time activation)
      - Bootstrap admin bypass via BOOTSTRAP_ADMIN_EMAIL env var
    """
    token = _get_token_from_request(request)
    if not token:
        _log.warning("auth: rejected unauthenticated request path=%s", request.url.path)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    claims = verify_firebase_token(token)
    uid = claims["uid"]
    email = claims.get("email", "")

    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE uid = $1", uid)

    if row is None:
        # Check bootstrap admin bypass
        settings = get_settings()
        if settings.bootstrap_admin_email and email == settings.bootstrap_admin_email:
            _log.warning("signup gate: bootstrap admin activation uid=%s", uid)
            row = await _create_user(pool, uid, claims, system_role=SystemRole.PLATFORM_ADMIN)
        else:
            # Check signup_requests
            sr = await pool.fetchrow("SELECT * FROM signup_requests WHERE uid = $1", uid)
            if sr is None:
                _log.info("signup gate: no user row and no signup request uid=%s -> SignupRequired", uid)
                raise SignupRequired()
            sr_status = sr["status"]
            if sr_status == "pending":
                _log.debug("signup gate: signup request pending uid=%s", uid)
                raise SignupPending()
            if sr_status == "rejected":
                _log.warning("signup gate: rejected signup request retried login uid=%s", uid)
                raise SignupRejected()
            # Approved — create user row (one-time activation)
            row = await _create_user(pool, uid, claims, system_role=sr["requested_role"])

    user = dict(row)
    if user.get("actor_id") is not None:
        user["actor_id"] = str(user["actor_id"])
    if user.get("status") == "deactivated":
        _log.warning("signup gate: deactivated account attempted login uid=%s", uid)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")

    # Ensure user has a default assistant (auto-creates on first login)
    try:
        from app.assistant_provisioning import ensure_user_assistant
        await ensure_user_assistant(
            user_uid=user["uid"],
            user_actor_id=user["actor_id"],
            user_display_name=user.get("display_name", ""),
            user_email=user.get("email", ""),
        )
    except Exception as exc:
        _log.debug("SHCA auto-provision skipped: %s", exc)

    # Explainability preferences (tooltips/hover/startup wizard) — attached here
    # so every route that already depends on get_current_user threads them into
    # base.html without a separate per-page fetch.
    from app.services.user_preferences import get_preferences
    user["preferences"] = await get_preferences(user["uid"])

    return user


async def _create_user(pool, uid: str, claims: dict, system_role: str = SystemRole.PLATFORM_MEMBER):
    """Create a new user row + log USER_CREATED event. Returns the user row."""
    email = claims.get("email", "")
    # Password-auth tokens carry no "name" claim (no profile source like Google's OAuth
    # profile) — fall back to the email local-part so display_name is never blank.
    display_name = claims.get("name") or (email.split("@")[0] if email else "")
    photo_url = claims.get("picture", "")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO users (uid, email, display_name, photo_url, system_role, status)
                   VALUES ($1, $2, $3, $4, $5, 'active')
                   ON CONFLICT (uid) DO NOTHING""",
                uid, email, display_name, photo_url, system_role,
            )
            row = await conn.fetchrow("SELECT * FROM users WHERE uid = $1", uid)
            # Enroll the ledger `actors` principal for this human — the design
            # intent of migration 028 ("new rows inserted by application on user
            # creation"). Without it, human_actor() returns users.actor_id but the
            # emit-time provenance audit finds no actors row → "unregistered actor"
            # warnings and human web actions never resolve to a named principal.
            # ON CONFLICT DO NOTHING guards both the id PK and the uid unique index.
            await conn.execute(
                """INSERT INTO actors (id, type, uid, display_name)
                   VALUES ($1, 'user', $2, $3)
                   ON CONFLICT DO NOTHING""",
                row["actor_id"], uid, display_name or email or uid,
            )
            await append_event(
                conn,
                event_type=USER_CREATED,
                payload={
                    "uid": uid,
                    "email": email,
                    "display_name": display_name,
                    "system_role": system_role,
                    "provider": claims.get("firebase", {}).get("sign_in_provider", ""),
                },
                actor_id=row["actor_id"],
                actor_type="system",
            )
    _log.info("user created uid=%s system_role=%s", uid, system_role)
    return row


# ── Role dependencies ────────────────────────────────────────────────────────

async def require_admin(request: Request) -> dict:
    """Dependency — requires system_role == platform.admin."""
    user = await get_current_user(request)
    if user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning(
            "require_admin: denied uid=%s role=%s path=%s",
            user.get("uid"), user.get("system_role"), request.url.path,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


async def require_editor(request: Request) -> dict:
    """Dependency — requires system_role in (platform.admin, platform.editor)."""
    user = await get_current_user(request)
    if user.get("system_role") not in SystemRole.PRIVILEGED:
        _log.warning(
            "require_editor: denied uid=%s role=%s path=%s",
            user.get("uid"), user.get("system_role"), request.url.path,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Editor access required")
    return user


async def get_goal_role(uid: str, pid: str) -> Optional[str]:
    """Returns the user's role in a goal (GoalRole.GOAL_ADMIN|GoalRole.GOAL_MEMBER) or None.

    The Platform Research Council goal (__platform__) is membership-free and
    admins-only: any system admin has role goal.manager there (no goal_members row),
    everyone else None. Centralized here so every goal_role/access check
    (goal_thread page, WS, etc.) treats it consistently."""
    pool = get_pool()
    if pid == "__platform__":
        srole = await pool.fetchval("SELECT system_role FROM users WHERE uid = $1", uid)
        return GoalRole.GOAL_ADMIN if srole == SystemRole.PLATFORM_ADMIN else None
    return await pool.fetchval(
        "SELECT role FROM goal_members WHERE goal_id = $1 AND uid = $2",
        pid, uid,
    )


async def get_agent_actor_id(agent_id: str):
    """Returns the UUID actor_id for an agent, or None if not found."""
    pool = get_pool()
    return await pool.fetchval(
        "SELECT actor_id FROM agents WHERE id = $1", agent_id,
    )


async def require_internal_key(request: Request):
    """Dependency for internal PEP endpoints — validates X-Internal-Key header."""
    from app.config import get_settings
    key = request.headers.get("X-Internal-Key", "")
    if not key or key != get_settings().internal_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key",
        )


async def require_goal_member(request: Request, pid: str) -> dict:
    user = await get_current_user(request)
    # The Platform Research Council goal is admins-only and not membership-based:
    # every admin can access it, and new admins see it immediately (no goal_members
    # sync). Non-admins are refused outright.
    if pid == "__platform__":
        if user.get("system_role") != SystemRole.PLATFORM_ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")
        user["goal_role"] = GoalRole.GOAL_ADMIN
        return user
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        if user.get("system_role") == SystemRole.PLATFORM_ADMIN:
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a goal member")
    user["goal_role"] = role
    return user
