"""Central authentication for the internal / agent-facing API surface (`/internal/*`).

SINGLE SOURCE OF TRUTH. Every `/internal/*` router delegates here, so the whole
agent/tool trust model is visible and changeable in ONE place. Previously each
router copy-pasted a `Bearer {openclaw_auth_token}` check (~10 sites); they now all
call `verify_internal_bearer()`.

Roadmap — see `SHARED_TOKEN_SECURITY_FIX_PLAN.md` (workspace root):
  * Phase 0 (CURRENT): consolidate the shared-bearer check here — BEHAVIOUR-PRESERVING.
    Every caller still authenticates exactly as before (shared token, HTTP 401 on
    mismatch). The only change is that the logic lives here instead of in 10 copies.
  * Phase 1+: add per-agent JWT verification (`AgentPrincipal`), a distinct
    gateway-service token for the infra callers (plan §9), ownership-join helpers,
    and dual-accept + shadow enforcement — all landing in THIS module without
    touching call sites. Not implemented yet; do not add untested behaviour to prod.
"""
from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass
from typing import Optional

import jwt

from fastapi import Depends, HTTPException, Request

from app.config import get_settings

_log = logging.getLogger(__name__)


# ── Phase 0 — shared-bearer verification (the current trust model) ───────────

def _bearer_matches(auth_header: str, token: str) -> bool:
    """Timing-safe check that `auth_header` == `Bearer {token}`.

    Functionally identical to the previous `!=` comparisons, but constant-time so
    the shared token can't be recovered via a response-timing oracle. Returns False
    when the token is unset (fail-closed) rather than accepting `Bearer `.
    """
    if not token:
        return False
    return hmac.compare_digest(auth_header, f"Bearer {token}")


_LEGACY_TOKEN_LOG_INTERVAL = 60.0  # seconds; per-path throttle for the deprecation log
_legacy_token_last_logged: dict[str, float] = {}


def _emit_deprecated_legacy_token(path: str, method: str) -> None:
    """Log a shared-token /internal use at most once per path per interval, so the
    cutover gets the caller inventory without flooding (the shared token is still the
    primary auth path)."""
    now = time.monotonic()
    last = _legacy_token_last_logged.get(path, 0.0)
    if now - last < _LEGACY_TOKEN_LOG_INTERVAL:
        return
    _legacy_token_last_logged[path] = now
    _log.info("internal_auth: deprecated_legacy_token path=%s method=%s", path, method)


def verify_internal_bearer(request: Request) -> None:
    """Authenticate an `/internal/*` caller against the shared OpenClaw token.

    Raises HTTP 401 on mismatch — unchanged from the per-router checks this
    replaces. This is the ONLY place the shared-token comparison lives; when
    Phase 1 lands, per-agent JWT acceptance is added here (dual-accept) with no
    change required at any call site.

    Usable two ways:
      * inline:      `verify_internal_bearer(request)`  (drop-in for old `_verify_auth`)
      * dependency:  `dependencies=[Depends(verify_internal_bearer)]`
    """
    settings = get_settings()
    auth = request.headers.get("Authorization", "")
    # Phase 1 DUAL-ACCEPT: a valid per-agent JWT is accepted when the feature is
    # on. Goal/ownership scoping is enforced separately (require_goal_ownership);
    # this is only the authentication gate. Flag OFF -> pure Phase-0 shared-token
    # check, unchanged.
    if settings.agent_scoped_tokens_enabled:
        _tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if _tok and _decode_agent_token(_tok) is not None:
            return
    if not _bearer_matches(auth, settings.openclaw_auth_token):
        raise HTTPException(status_code=401, detail="Invalid agent token")
    # P0-A.2 CUTOVER: the shared token matched. Once every agent-facing caller
    # sends a per-agent JWT (verify via `deprecated_legacy_token` telemetry ~= 0),
    # flip AGENT_REJECT_SHARED_TOKEN=true so a leaked/echoed shared token is no
    # longer a skeleton key on /internal/*. The bootstrap mint endpoint keeps its
    # own path (verify_bootstrap_bearer) so JWT minting still works. Gated by a
    # flag (default OFF) so enabling is deliberate — flipping before a caller is
    # migrated would 401 it (and pause-check fails closed).
    if settings.agent_reject_shared_token:
        raise HTTPException(
            status_code=401,
            detail="shared token not accepted on this endpoint; use a per-agent JWT",
        )
    # Still on the legacy SHARED token while per-agent JWTs are enabled. Telemetry
    # so the cutover can see which callers still fall back before we flip the reject.
    # Throttled per-path; non-fatal, log-only.
    if settings.agent_scoped_tokens_enabled:
        try:
            _emit_deprecated_legacy_token(request.url.path, request.method)
        except Exception:
            pass


def verify_bootstrap_bearer(request: Request) -> None:
    """Auth for BOOTSTRAP / backend endpoints that must keep accepting the shared
    (service) token even after AGENT_REJECT_SHARED_TOKEN is on.

    The one that needs this is POST /internal/agent-token: the gateway presents
    the shared token to MINT a per-agent JWT (chicken-and-egg — it can't have a
    JWT before it mints one). Accepts a valid per-agent JWT OR the shared token,
    and never applies the reject flag. NOTE: an agent that can still read the
    shared secret (via the VM SA / metadata) could mint a JWT for an (agent,goal)
    the mint endpoint validates — fully removing agent reach to this token is the
    isolation follow-up (B1); combined with the SA scope-down it is much reduced."""
    settings = get_settings()
    auth = request.headers.get("Authorization", "")
    if settings.agent_scoped_tokens_enabled:
        _tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        _cl = _decode_agent_token(_tok) if _tok else None
        # Only a per-agent ('agent') JWT may mint here. A 'human_local' token must
        # NOT be able to mint agent JWTs at /internal/agent-token (§14.1 / §14.6
        # bootstrap-mint guard) — it falls through to the shared-token check, which
        # it won't satisfy, so it 401s.
        if _cl is not None and _cl.get("typ") == "agent":
            return
    if not _bearer_matches(auth, settings.openclaw_auth_token):
        raise HTTPException(status_code=401, detail="Invalid agent token")


# Convenience dependency object for route decorators.
require_internal_bearer = Depends(verify_internal_bearer)

# ── Phase 1 — per-agent scoped tokens (JWT) ──────────────────────────────────
# Minted by the control plane (signing key never leaves the backend), carried by agents, verified
# here. When AGENT_SCOPED_TOKENS_ENABLED is off this is dormant (verify_internal_bearer is
# the pure Phase-0 shared-token check). When on, verify_internal_bearer ALSO accepts a valid
# per-agent JWT (dual-accept); goal scoping is enforced by handlers calling
# require_goal_ownership (SHADOW unless AGENT_AUTHZ_ENFORCE=true).

_JWT_ALG = "HS256"


@dataclass(frozen=True)
class AgentPrincipal:
    """Decoded identity of a per-agent JWT caller."""
    agent_id: str
    goal_id: str
    scopes: tuple[str, ...]
    # DB id of the agent_instances row this token was minted for (None for GO agents,
    # which live on goals.go_agent_id, not agent_instances). Used to close the
    # revocation gap: a JWT stays valid until exp (~1h) even if the instance is
    # destroyed mid-life, so the accept path re-checks liveness (see check_principal_live).
    agent_instance_id: Optional[str] = None
    # 'agent' (per-agent JWT) or 'human_local' (Local Execution Mode token, §14.1).
    token_type: str = "agent"
    # Story a human-local token is scoped to (None for per-agent tokens).
    story_id: Optional[str] = None


def mint_agent_token(
    agent_id: str,
    goal_id: str,
    scopes=(),
    ttl_seconds: int = 3600,
    agent_instance_id: Optional[str] = None,
) -> str:
    """Mint a short-lived, goal-scoped JWT for one agent. Raises if no signing key."""
    key = get_settings().agent_token_signing_key
    if not key:
        raise RuntimeError("agent_token_signing_key not configured")
    now = int(time.time())
    payload = {
        "typ": "agent",
        "sub": agent_id,
        "goal_id": goal_id,
        "scopes": list(scopes or []),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    if agent_instance_id:
        payload["aiid"] = str(agent_instance_id)
    return jwt.encode(payload, key, algorithm=_JWT_ALG)


def mint_human_local_token(
    agent_id: str,
    goal_id: str,
    story_id: str,
    agent_instance_id: Optional[str] = None,
    ttl_seconds: int = 3600,
) -> str:
    """Mint a short-lived STORY-scoped human-local JWT for the Local Execution
    extension (§14.1). Same machinery as mint_agent_token but typ='human_local'
    + a story_id claim; carries the SHCA instance id (aiid) so events attribute
    to the assistant on_behalf_of the human. Verified through the SAME single
    decoder — it authenticates /internal/*, is goal+story scoped by ownership_guard,
    is 403'd at /approvals/decide (human gate preserved), and CANNOT mint agent
    JWTs (verify_bootstrap_bearer only accepts typ='agent')."""
    key = get_settings().agent_token_signing_key
    if not key:
        raise RuntimeError("agent_token_signing_key not configured")
    # aiid is REQUIRED for a human-local token — without it request_actor can't
    # resolve obo (owner) and would misattribute the action to SYSTEM (§14.1).
    if not agent_instance_id:
        raise RuntimeError("human_local token requires agent_instance_id for obo attribution")
    now = int(time.time())
    payload = {
        "typ": "human_local",
        "sub": agent_id,
        "goal_id": goal_id,
        "story_id": story_id,
        "scopes": [],
        "aiid": str(agent_instance_id),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return jwt.encode(payload, key, algorithm=_JWT_ALG)


def mint_ext_session_token(uid: str, ttl_seconds: int = 3600) -> str:
    """CP-minted session token for the Local Execution extension — represents the
    HUMAN (uid). Short-lived; the extension re-mints via its refresh handle
    (option B, §6). Distinct typ='ext_session': it is NOT accepted by
    _decode_agent_token (agent endpoints) or verify_bootstrap_bearer — only
    decode_ext_session_uid reads it, at the human-facing local endpoints."""
    key = get_settings().agent_token_signing_key
    if not key:
        raise RuntimeError("agent_token_signing_key not configured")
    now = int(time.time())
    return jwt.encode(
        {"typ": "ext_session", "sub": uid, "iat": now, "exp": now + int(ttl_seconds)},
        key, algorithm=_JWT_ALG,
    )


def decode_ext_session_uid(token: str) -> Optional[str]:
    """Verify an ext_session token → uid, else None. Kept separate from
    _decode_agent_token so an ext_session token can never authenticate an agent
    endpoint or mint agent JWTs (typ isolation)."""
    key = get_settings().agent_token_signing_key
    if not key or not token:
        return None
    try:
        claims = jwt.decode(token, key, algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "ext_session":
        return None
    return str(claims.get("sub") or "") or None


def _decode_agent_token(token: str) -> Optional[dict]:
    """Verify signature + expiry; return claims or None (never raises). algorithms
    pinned to HS256 to prevent alg-confusion attacks."""
    key = get_settings().agent_token_signing_key
    if not key or not token:
        return None
    try:
        claims = jwt.decode(token, key, algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        return None
    # Accept per-agent ('agent') AND Local Execution human-local ('human_local')
    # tokens through this SINGLE decoder — every consumer (verify_internal_bearer,
    # ownership_guard, request_actor, the /approvals 403) keys off it, so widening
    # here makes goal/story scoping + obo attribution + the approval gate all work
    # for the local token with no side path (§14.1).
    return claims if claims.get("typ") in ("agent", "human_local") else None


def get_agent_principal(request: Request) -> Optional[AgentPrincipal]:
    """Decode the caller's per-agent JWT into an AgentPrincipal, or None for a legacy
    (shared-token) caller. Pure/sync, no DB, never raises — authentication already
    happened in verify_internal_bearer; this only exposes the identity for scoping."""
    if not get_settings().agent_scoped_tokens_enabled:
        return None
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    claims = _decode_agent_token(token) if token else None
    if not claims:
        return None
    return AgentPrincipal(
        agent_id=str(claims.get("sub") or ""),
        goal_id=str(claims.get("goal_id") or ""),
        scopes=tuple(claims.get("scopes") or ()),
        agent_instance_id=(str(claims["aiid"]) if claims.get("aiid") else None),
        token_type=str(claims.get("typ") or "agent"),
        story_id=(str(claims["story_id"]) if claims.get("story_id") else None),
    )


async def check_principal_live(principal: AgentPrincipal) -> Optional[str]:
    """Re-validate at request time that the token's goal (and instance, if any) is
    still alive — closes the ~1h revocation gap between mint and exp. Returns None if
    live, else a short reason string ('goal_inactive' / 'instance_inactive'). DB-backed;
    callers decide whether to block (enforce) or log (shadow). Never raises — on any DB
    error returns None (fail-open; auth already happened, this is only revocation)."""
    try:
        from app.services.goal_guard import goal_is_dead  # lazy: avoid import cycle
        from app.database import get_pool
        if principal.goal_id and await goal_is_dead(principal.goal_id):
            return "goal_inactive"
        if principal.agent_instance_id:
            row = await get_pool().fetchrow(
                "SELECT status FROM agent_instances WHERE id = $1",
                principal.agent_instance_id,
            )
            if row is not None and row["status"] != "active":
                return "instance_inactive"
    except Exception:
        return None
    return None


def require_goal_ownership(request: Request, target_goal_id: str) -> None:
    """Enforce that a per-agent JWT caller only acts within its own goal (fixes the
    cross-goal IDOR, audit #2). Legacy shared-token callers have no principal -> no-op
    (dual-accept). SHADOW by default (logs a would-be denial without blocking); set
    AGENT_AUTHZ_ENFORCE=true to turn a mismatch into a 403. Call once the target
    entity's goal_id is known."""
    principal = get_agent_principal(request)
    if principal is None or not target_goal_id:
        return
    if principal.goal_id == target_goal_id:
        return
    if get_settings().agent_authz_enforce:
        _log.warning("internal_auth: DENY cross-goal agent=%s token_goal=%s target_goal=%s",
                     principal.agent_id, principal.goal_id, target_goal_id)
        raise HTTPException(status_code=403, detail="cross-goal access denied")
    _log.warning("internal_auth: SHADOW cross-goal (would-403) agent=%s token_goal=%s target_goal=%s",
                 principal.agent_id, principal.goal_id, target_goal_id)


def require_story_ownership(request: Request, target_story_id: str) -> None:
    """Enforce that a STORY-scoped human-local token only acts on ITS story
    (§14.1 story-level scoping — ownership_guard is otherwise goal-only). No-op for
    per-agent tokens (no story_id) and legacy callers. SHADOW unless AGENT_AUTHZ_ENFORCE
    (mirrors require_goal_ownership)."""
    principal = get_agent_principal(request)
    if principal is None or not principal.story_id or not target_story_id:
        return
    if principal.story_id == target_story_id:
        return
    if get_settings().agent_authz_enforce:
        _log.warning("internal_auth: DENY cross-story agent=%s token_story=%s target_story=%s",
                     principal.agent_id, principal.story_id, target_story_id)
        raise HTTPException(status_code=403, detail="cross-story access denied")
    _log.warning("internal_auth: SHADOW cross-story (would-403) agent=%s token_story=%s target_story=%s",
                 principal.agent_id, principal.story_id, target_story_id)


async def ownership_guard(request: Request) -> None:
    """FastAPI dependency — enforce goal ownership for /internal/* endpoints, resolving
    the target goal from whatever the path/query carries: `pid`/`goal_id` (direct),
    `tid` (-> tasks.goal_id), `story_id`/`sid` (-> stories.goal_id), or a `goal_id`
    query param. No-op for legacy/browser callers (no per-agent JWT) and for routes with
    no resolvable goal (those handlers enforce their own). SHADOW unless AGENT_AUTHZ_ENFORCE."""
    principal = get_agent_principal(request)
    if principal is None:
        return
    # Revocation check: the token's own goal/instance must still be live. Closes the
    # ~1h gap where a JWT outlives a deleted goal or destroyed instance. SHADOW unless
    # AGENT_AUTHZ_ENFORCE (mirrors the cross-goal check).
    dead = await check_principal_live(principal)
    if dead is not None:
        if get_settings().agent_authz_enforce:
            _log.warning("internal_auth: DENY revoked agent=%s goal=%s reason=%s",
                         principal.agent_id, principal.goal_id, dead)
            raise HTTPException(status_code=403, detail="agent token revoked")
        _log.warning("internal_auth: SHADOW revoked (would-403) agent=%s goal=%s reason=%s",
                     principal.agent_id, principal.goal_id, dead)
    from app.database import resolve_goal_id, get_pool  # lazy: avoid import cycle
    pp = request.path_params or {}
    goal = None
    raw = pp.get("pid") or pp.get("goal_id")
    if raw:
        try:
            goal = await resolve_goal_id(raw)
        except Exception:
            goal = raw
    elif pp.get("tid"):
        goal = await get_pool().fetchval("SELECT goal_id FROM tasks WHERE id = $1", pp["tid"])
    elif pp.get("story_id") or pp.get("sid"):
        goal = await get_pool().fetchval(
            "SELECT goal_id FROM stories WHERE id = $1", pp.get("story_id") or pp.get("sid"))
    if goal is None:
        q = request.query_params.get("goal_id")
        if q:
            try:
                goal = await resolve_goal_id(q)
            except Exception:
                goal = q
    if goal:
        require_goal_ownership(request, goal)
    # Story-level scope for human-local tokens (no-op for per-agent/legacy callers).
    _sid = pp.get("story_id") or pp.get("sid")
    if _sid:
        require_story_ownership(request, _sid)
