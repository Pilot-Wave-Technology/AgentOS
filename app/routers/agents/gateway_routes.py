"""Gateway discovery and management endpoints.

JSON API:
  GET  /api/gateway/agents                              — list gateway agents
  GET  /api/gateway/models                              — list gateway models
  GET  /api/gateway/tools                               — get tools catalog
  GET  /api/gateway/skills                              — get skills status
  POST /api/agents/{agent_id}/sync-gateway              — sync agent to gateway
  POST /api/gateway/skills/install                      — install a skill
  GET  /api/gateway/agents/{agent_id}/files             — list agent workspace files
  GET  /api/gateway/agents/{agent_id}/files/{filename}  — get a file
  PUT  /api/gateway/agents/{agent_id}/files/{filename}  — write a file (+ mirror to DB)
"""
import asyncio
import hashlib
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import require_admin, require_editor
from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError

router = APIRouter()
logger = logging.getLogger(__name__)


# -- Gateway discovery ---------------------------------------------------------

@router.get("/api/gateway/agents")
async def api_gateway_agents(user: dict = Depends(require_editor)):
    try:
        result = await gateway_admin.list_agents()
        logger.debug("Gateway agents listed by %s", user.get("actor_id"))
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to list gateway agents: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/gateway/models")
async def api_gateway_models(user: dict = Depends(require_editor)):
    try:
        result = await gateway_admin.list_models()
        logger.debug("Gateway models listed by %s", user.get("actor_id"))
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to list gateway models: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/gateway/tools")
async def api_gateway_tools(agent_id: str = "", user: dict = Depends(require_editor)):
    try:
        result = await gateway_admin.get_tools_catalog(agent_id or None)
        logger.debug("Gateway tools catalog fetched (agent_id=%s) by %s", agent_id or "all", user.get("actor_id"))
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to fetch gateway tools catalog: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/gateway/skills")
async def api_gateway_skills(agent_id: str = "", user: dict = Depends(require_editor)):
    try:
        result = await gateway_admin.get_skills_status(agent_id or None)
        logger.debug("Gateway skills status fetched (agent_id=%s) by %s", agent_id or "all", user.get("actor_id"))
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to fetch gateway skills status: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


# -- Gateway sync + file management -------------------------------------------

@router.post("/api/agents/{agent_id}/sync-gateway")
async def sync_agent_to_gateway(agent_id: str, request: Request, user: dict = Depends(require_admin)):
    """Ensure an agent exists on the gateway and sync all its files from the DB.

    Creates the agent on the gateway if it doesn't exist, then writes all
    agent_files (AGENTS.md, SOUL.md, etc.) to the gateway workspace.
    """
    sync_start = time.monotonic()
    pool = get_pool()
    agent = await pool.fetchrow("SELECT * FROM agents WHERE id = $1 AND status = 'active'", agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = dict(agent)

    gateway_agent_id = agent.get("gateway_agent_id") or agent_id
    model = agent.get("model", "google/gemini-2.5-flash")
    workspace = f"~/.openclaw/workspace-{gateway_agent_id}"
    tools_allow = agent.get("tools_allow", [])
    tools_param = {"allow": tools_allow} if tools_allow else None

    # Create agent on gateway (skip if already exists)
    try:
        await gateway_admin.call("agents.create", {
            "name": gateway_agent_id,
            "workspace": workspace,
            "model": model,
            **({"tools": tools_param} if tools_param else {}),
        })
        logger.info("Created gateway agent %s", gateway_agent_id)
    except GatewayError as exc:
        if "already exists" not in str(exc):
            raise HTTPException(status_code=500, detail=f"Gateway error: {exc}")
        # Already exists — update model/tools
        try:
            await gateway_admin.call("agents.update", {
                "agentId": gateway_agent_id,
                "model": model,
                **({"tools": tools_param} if tools_param else {}),
            })
        except Exception:
            pass

    # Wire up memory_v2 for SHCA/GO agents — non-fatal if memory_v2 not yet deployed
    try:
        agent_type = agent.get("type") or ""
        if agent_type in ("SHCA", "GO"):
            from app.memory_v2 import provisioning as _mv2_provisioning
            owner_uid = agent.get("owner_actor_id")
            owner_uid_str = str(owner_uid) if owner_uid else None
            await _mv2_provisioning.setup_memory_v2_for_agent(
                agent_id=gateway_agent_id, agent_type=agent_type, user_uid=owner_uid_str,
            )
    except Exception:
        logger.exception("memory_v2 setup failed for admin-sync of %s — non-fatal", gateway_agent_id)

    # Sync all agent files from DB to gateway workspace (parallel)
    files = await pool.fetch(
        "SELECT filename, content FROM agent_files WHERE agent_id = $1", agent_id,
    )
    if files:
        results = await asyncio.gather(
            *[gateway_admin.set_agent_file(gateway_agent_id, f["filename"], f["content"]) for f in files],
            return_exceptions=True,
        )
        synced = [f["filename"] for f, r in zip(files, results) if not isinstance(r, Exception)]
        for f, r in zip(files, results):
            if isinstance(r, Exception):
                logger.warning("Failed to sync %s to %s: %s", f["filename"], gateway_agent_id, r)
    else:
        synced = []

    logger.info("Synced %d files to gateway agent %s: %s", len(synced), gateway_agent_id, synced)

    config_version = agent.get("config_version")
    duration_ms = int((time.monotonic() - sync_start) * 1000)

    async with pool.acquire() as conn:
        async with conn.transaction():
            if config_version is not None:
                await conn.execute(
                    """UPDATE agents
                       SET gateway_synced_version = $1, gateway_synced_at = now()
                       WHERE id = $2""",
                    config_version, agent_id,
                )
                await conn.execute(
                    """INSERT INTO agent_gateway_bindings
                           (agent_id, gateway_agent_id, workspace,
                            synced_config_version, last_synced_at, last_sync_attempted_at, last_sync_error)
                       VALUES ($1, $2, $3, $4, now(), now(), NULL)
                       ON CONFLICT (agent_id) DO UPDATE SET
                           synced_config_version  = $4,
                           last_synced_at         = now(),
                           last_sync_attempted_at = now(),
                           last_sync_error        = NULL""",
                    agent_id, gateway_agent_id, workspace, config_version,
                )
            if synced:
                await conn.execute(
                    """UPDATE agent_files SET gateway_synced_at = now()
                       WHERE agent_id = $1 AND filename = ANY($2)""",
                    agent_id, synced,
                )

    await pool.execute(
        """INSERT INTO agent_sync_log
               (agent_id, from_version, to_version, files_synced, success, duration_ms, triggered_by)
           VALUES ($1, $2, $3, $4, TRUE, $5, $6)""",
        agent_id, config_version, config_version, synced, duration_ms, user["actor_id"],
    )

    return JSONResponse({
        "ok": True,
        "agent_id": agent_id,
        "gateway_agent_id": gateway_agent_id,
        "files_synced": synced,
    })


@router.post("/api/gateway/skills/install")
async def api_install_skill(request: Request, user: dict = Depends(require_admin)):
    """Install a skill on the gateway. Admin-only — a mutation on the live
    gateway, same tier as other Agent Registry write actions."""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Skill name is required")
    try:
        result = await gateway_admin.install_skill(name)
        logger.info("Skill installed: name=%s by %s", name, user.get("actor_id"))
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to install skill %s: %s", name, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/gateway/agents/{agent_id}/files")
async def api_agent_files(agent_id: str, user: dict = Depends(require_editor)):
    try:
        result = await gateway_admin.list_agent_files(agent_id)
        logger.debug("Files listed for gateway agent %s by %s", agent_id, user.get("actor_id"))
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to list files for gateway agent %s: %s", agent_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/gateway/agents/{agent_id}/files/{filename}")
async def api_get_agent_file(agent_id: str, filename: str, user: dict = Depends(require_editor)):
    try:
        result = await gateway_admin.get_agent_file(agent_id, filename)
        logger.debug("File %s fetched for gateway agent %s by %s", filename, agent_id, user.get("actor_id"))
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to fetch file %s for gateway agent %s: %s", filename, agent_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.put("/api/gateway/agents/{agent_id}/files/{filename}")
async def api_set_agent_file(
    agent_id: str, filename: str, request: Request, user: dict = Depends(require_admin),
):
    """Write an agent workspace file on the gateway (+ mirror to DB). Admin-only
    — a mutation on the live gateway, same tier as other Agent Registry write
    actions (platform.editor has read-only access to the Agent Registry)."""
    body = await request.json()
    content = body.get("content", "")
    try:
        result = await gateway_admin.set_agent_file(agent_id, filename, content)

        # Also persist to PostgreSQL so task controller instances get updated files
        pool = get_pool()
        # agent_id from gateway may differ from DB id — resolve via gateway_agent_id
        db_agent_id = await pool.fetchval(
            "SELECT id FROM agents WHERE gateway_agent_id = $1 AND status = 'active'", agent_id,
        )
        if not db_agent_id:
            # Try direct match
            db_agent_id = await pool.fetchval(
                "SELECT id FROM agents WHERE id = $1 AND status = 'active'", agent_id,
            )
        if db_agent_id:
            content_hash = hashlib.md5(content.encode()).hexdigest()
            file_size_bytes = len(content.encode())
            await pool.execute(
                """INSERT INTO agent_files
                       (agent_id, filename, content, written_by, content_hash, file_size_bytes)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (agent_id, filename) DO UPDATE
                       SET content = $3, written_by = $4, content_hash = $5, file_size_bytes = $6""",
                db_agent_id, filename, content,
                user.get("actor_id", user.get("uid", "")), content_hash, file_size_bytes,
            )
            logger.info("Synced %s to DB for agent %s (gateway: %s)", filename, db_agent_id, agent_id)

        return JSONResponse(result)
    except Exception as exc:
        logger.error("Failed to set file %s for gateway agent %s: %s", filename, agent_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)
