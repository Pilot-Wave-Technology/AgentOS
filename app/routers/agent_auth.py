"""Per-agent token minting endpoint (shared-token security fix, Phase 1).

`POST /internal/agent-token` — the gateway calls this at agent-session start,
authenticated with the gateway-service (currently shared) token, to exchange a
gateway agent id for a short-lived, per-agent, goal-scoped JWT. the control plane is the SOLE
minter: the signing key never leaves the backend, so a compromised gateway host
cannot forge tokens for arbitrary agents/goals.

Additive + INERT until the signing key is configured AND the gateway is wired to
call this (Phase 1 slice 2). Nothing accepts the minted JWT yet — `/internal/*`
routers still authenticate via the shared bearer — so this endpoint changes no
existing behaviour. See SHARED_TOKEN_SECURITY_FIX_PLAN.md.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.database import get_pool, resolve_goal_id
from app.internal_auth import mint_agent_token, verify_bootstrap_bearer
from app.services.goal_guard import goal_is_dead

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal")

_TOKEN_TTL_SECONDS = 3600


@router.post("/agent-token")
async def issue_agent_token(request: Request):
    """Mint a per-agent JWT for (agent, goal). Backend-to-backend: authenticated by
    the gateway-service token, NOT by an agent — an agent must never mint its own.

    Uses verify_bootstrap_bearer (not verify_internal_bearer) so it KEEPS accepting
    the shared/service token even after AGENT_REJECT_SHARED_TOKEN is on — the
    gateway must present the shared token here to obtain its first JWT (bootstrap)."""
    verify_bootstrap_bearer(request)

    if not get_settings().agent_token_signing_key:
        # Deferred config: minting not enabled yet. Callers fall back to the shared
        # bearer, so a 503 here is safe and self-describing.
        raise HTTPException(status_code=503, detail="agent token minting not configured")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    agent_id = (body.get("agent_id") or body.get("gateway_agent_id") or "").strip()
    goal_id = (body.get("goal_id") or "").strip()
    if not agent_id or not goal_id:
        raise HTTPException(status_code=400, detail="agent_id and goal_id are required")

    goal_id = await resolve_goal_id(goal_id)
    # Revocation at the source: never mint for a non-active goal.
    if await goal_is_dead(goal_id):
        raise HTTPException(status_code=404, detail="goal not active")

    agent_type, agent_instance_id = await _resolve_agent_type(agent_id, goal_id)
    if agent_type is None:
        # The agent must genuinely belong to the goal it's asking to act in.
        raise HTTPException(status_code=404, detail="agent not found for goal")

    scopes = (agent_type.lower(),)  # coarse per-type scope; refined when enforcement lands
    token = mint_agent_token(
        agent_id, goal_id, scopes,
        ttl_seconds=_TOKEN_TTL_SECONDS,
        agent_instance_id=agent_instance_id,
    )
    _log.info("agent-token issued: agent=%s goal=%s type=%s", agent_id, goal_id, agent_type)
    return {"token": token, "expires_in": _TOKEN_TTL_SECONDS, "goal_id": goal_id, "scopes": list(scopes)}


async def _resolve_agent_type(gateway_agent_id: str, goal_id: str):
    """Return `(agent_type, agent_instance_id)` IFF the agent genuinely belongs to
    `goal_id`, else `(None, None)`.

    Handles the GO (goals.go_agent_id — no instance row, so instance id is None) and
    instance agents (agent_instances). Matching is case-insensitive and prefix-tolerant
    because the gateway lowercases and truncates agent ids (~63 chars) while the DB
    stores the full id. The instance id rides into the JWT so the accept path can
    re-check the instance is still active (revocation).
    """
    pool = get_pool()
    go = await pool.fetchrow(
        "SELECT 1 FROM goals WHERE id = $1 AND status = 'active' "
        "AND lower(go_agent_id) = lower($2)",
        goal_id, gateway_agent_id,
    )
    if go:
        return "GO", None
    inst = await pool.fetchrow(
        """SELECT ai.id, a.type
             FROM agent_instances ai
             JOIN agents a ON a.id = ai.template_id
            WHERE ai.goal_id = $1 AND ai.status = 'active'
              AND (lower(ai.gateway_agent_id) = lower($2)
                   -- prefix match ONLY for gateway-truncated ids (~63 chars); uses
                   -- left()= not LIKE, so %/_ in $2 are literal (no LIKE-injection:
                   -- agent_id='%' can't wildcard-match every instance). length gate
                   -- stops short/common prefixes (e.g. 'go-') from broad-matching.
                   OR (length($2) >= 60
                       AND left(lower(ai.gateway_agent_id), length($2)) = lower($2)))
            ORDER BY (lower(ai.gateway_agent_id) = lower($2)) DESC, ai.created_at DESC
            LIMIT 1""",
        goal_id, gateway_agent_id,
    )
    if inst:
        return inst["type"], str(inst["id"])
    return None, None
