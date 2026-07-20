"""Internal endpoints consumed by the OpenClaw custom-tools plugin.

After phase 4, the plugin executes tools locally via its capability runtime —
there is no longer an /execute endpoint here. This module serves:

    GET  /internal/custom-tools                          — list of enabled tools
    GET  /internal/custom-tools/credentials/by-name/{n}  — plaintext credential
                                                            (for secrets.get)
    POST /internal/custom-tools/audit                    — batched capability
                                                            events from the
                                                            runtime audit pipe;
                                                            forwarded into the
                                                            existing ledger as
                                                            tool.capability_called
                                                            events

All Bearer-authed with the same token as `/internal/go/*`.

Rate limiting: removed in phase 4. The execute path it was guarding is gone;
the credentials endpoint is fetched once per tool call per declared secret,
and gateway-level rate limiting on chat.send already bounds the upstream
traffic. Adding limits here would not protect against a compromised plugin
because that plugin already has Bearer-token access to everything else.
"""
import asyncio
import json
import logging
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Any, Optional

from app.config import get_settings
from app.database import get_pool
from app.ledger.emit import emit_event
from app.ledger.schema import TOOL_CAPABILITY_CALLED
from app.services.custom_tools.credential import decrypt_value
from app.services.custom_tools.crud import list_custom_tools

# Same system-actor convention used by app/services/dag_ingest.py,
# operator_provisioning.py, etc. Per-event payload carries the actual agent_id
# so attribution isn't lost.
from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID
from app.ledger.actors import instance_actor as _instance_actor

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/custom-tools")


from app.internal_auth import verify_internal_bearer as _verify_auth  # SSOT: app/internal_auth.py


@router.get("")
async def list_tools(request: Request):
    """Return all enabled custom tools as definitions for the OpenClaw plugin.

    Each row includes `source_code` and `manifest` so the plugin can execute
    the tool locally via its capability runtime.
    """
    _verify_auth(request)
    pool = get_pool()
    tools = await list_custom_tools(pool, status="enabled")
    defs = [
        {
            "name": t["name"],
            "label": t["label"],
            "description": t["description"],
            "parameters_schema": t["parameters_schema"],
            "tier": t["tier"],
            "source_code": t.get("source_code"),
            "manifest": t.get("manifest"),
        }
        for t in tools
    ]
    log.debug("internal/custom-tools list: %d enabled tools", len(defs))
    return JSONResponse(defs)


@router.get("/credentials/by-name/{credential_name}")
async def get_credential_by_name(credential_name: str, request: Request, tool: str = Query("")):
    """Return the plaintext credential value for the capability runtime's secrets.get().

    The capability runtime on the VM calls this per tool invocation when a tool
    uses secrets.get(name). The Fernet encryption key never leaves Cloud Run —
    decryption happens here, plaintext is returned over the Bearer-authed channel.

    Allowlist enforcement (which tool may request which credential) is done in
    the runtime's secrets capability against the tool's manifest; this endpoint
    only enforces the Bearer token. A compromised plugin could therefore fetch
    any credential — same blast radius as before phase 4, since the plugin
    already had Bearer access to every other internal endpoint.
    """
    _verify_auth(request)

    pool = get_pool()
    await _enforce_tool_grants_credential(pool, tool, credential_name)
    row = await pool.fetchrow(
        "SELECT id, credential_type, encrypted_value, header_name, status FROM tool_credentials WHERE name = $1",
        credential_name,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"credential {credential_name!r} not found")

    # Fail safe on a scaffolded-but-unfilled credential — the human hasn't entered
    # the secret yet (Settings → Credentials). Don't run the tool with a blank.
    if row["status"] == "pending":
        raise HTTPException(
            status_code=409,
            detail=f"credential {credential_name!r} is pending — its secret has not "
                   "been entered yet (Settings → Credentials)",
        )

    try:
        plaintext = decrypt_value(str(row["id"]), row["encrypted_value"])
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return JSONResponse({
        "name": credential_name,
        "credential_type": row["credential_type"],
        "header_name": row["header_name"],
        "value": plaintext,
    })


# ── Audit ingest ──────────────────────────────────────────────────────────────

class AuditEvent(BaseModel):
    ts: Optional[str] = None
    tool: Optional[str] = None
    agent_id: Optional[str] = None
    session_key: Optional[str] = None
    event: str
    outcome: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[dict[str, Any]] = None


class AuditBatch(BaseModel):
    events: list[AuditEvent]


@router.post("/audit", status_code=202)
async def ingest_audit(batch: AuditBatch, request: Request):
    """Accept a batch of capability-call events from the OpenClaw plugin and
    forward each one into the existing append-only ledger as a
    `tool.capability_called` event.

    Why the ledger instead of a dedicated table:
      - Already append-only with SHA-256 hash chaining (cryptographic integrity)
      - Already has correlation/causation chains
      - Projections (`task_activity`, etc.) can be built from these events
      - New event types are just new strings; no schema migration needed

    The plugin batches events on a short timer (every few seconds, or when the
    buffer fills). 202 because durability is best-effort — the plugin retries
    failed flushes from its local ring buffer. Per-event emit_event is itself
    fire-and-forget (never raises); a transient ledger outage drops the event
    rather than failing the batch.
    """
    _verify_auth(request)
    if not batch.events:
        return JSONResponse({"received": 0}, status_code=202)

    # Per-event emit_event is fire-and-forget by contract (never raises).
    # asyncio.gather lets transactions run concurrently instead of one-by-one
    # at the asyncpg pool level — a 200-event batch becomes one gather instead
    # of 200 sequential awaits. emit_event handles its own exceptions; gather
    # won't propagate any failure here.
    async def _emit_one(ev):
        # The calling agent instance is the actor (resolved from its gateway
        # agent id); system fallback if the instance can't be resolved.
        actor_id, actor_type = await _instance_actor(gateway_agent_id=ev.agent_id)
        await emit_event(
            event_type=TOOL_CAPABILITY_CALLED,
            payload={
                "tool":        ev.tool,
                "agent_id":    ev.agent_id,
                "session_key": ev.session_key,
                "event":       ev.event,
                "outcome":     ev.outcome,
                "reason":      ev.reason,
                "detail":      ev.detail,
                "ts":          ev.ts,
            },
            actor_id=actor_id,
            actor_type=actor_type,
        )

    await asyncio.gather(*(_emit_one(ev) for ev in batch.events))

    log.debug("audit ingest: forwarded %d event(s) to ledger", len(batch.events))
    return JSONResponse({"received": len(batch.events)}, status_code=202)


async def _enforce_tool_grants_credential(pool, tool: str, credential_name: str) -> None:
    """Server-side credential allowlist (audit #3). The calling tool's manifest
    (capabilities.secrets.names) must grant this credential name — otherwise a
    compromised plugin could fetch ANY credential by skipping its own client-side
    check. SHADOW by default (logs a would-be denial); AGENT_AUTHZ_ENFORCE=true turns
    it into a 403. A legit tool never requests a credential it doesn't declare, so
    enforcement does not break normal traffic."""
    granted = None
    if tool:
        trow = await pool.fetchrow(
            "SELECT manifest FROM tools WHERE name = $1 AND status = 'enabled'", tool)
        if trow and trow["manifest"] is not None:
            man = trow["manifest"]
            if isinstance(man, str):
                try:
                    man = json.loads(man)
                except Exception:
                    man = {}
            granted = (((man.get("capabilities") or {}).get("secrets") or {}).get("names")) or []
    if tool and granted is not None and credential_name in granted:
        return
    reason = "no tool identity" if not tool else "not in tool manifest grant"
    if get_settings().custom_tools_broker_enforce:
        log.warning("credential broker DENY: cred=%s tool=%s (%s)", credential_name, tool or "-", reason)
        raise HTTPException(status_code=403, detail=f"credential {credential_name!r} not granted to tool {tool or '?'}")
    log.warning("credential broker SHADOW (would-403): cred=%s tool=%s (%s)", credential_name, tool or "-", reason)
