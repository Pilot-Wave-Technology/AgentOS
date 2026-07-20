"""Agent provisioning — create an agent on the gateway and persist it to the DB."""
import asyncio
import hashlib
import logging
from typing import Any

from app.llm_models import normalize_model_id
from app.models.agent import AgentCreateRequest
from app.openclaw import gateway_admin as _default_gateway, GatewayError
from app.ledger.journal import append_event
from app.ledger.schema import AGENT_PROVISIONED
from .helpers import enroll_agent_principal, resolve_agent_tools

logger = logging.getLogger(__name__)


def _workspace_path(agent_id: str) -> str:
    return f"~/.openclaw/workspace-{agent_id}"


async def provision_agent(pool, req: AgentCreateRequest, actor_id: str, *, gateway=None) -> dict[str, Any]:
    """Create an agent on the gateway and persist it to the database.

    Raises:
        ValueError: An active agent with this ID already exists.
        GatewayError: Gateway rejected the create/update call.
        Exception: DB error — transaction is rolled back; gateway agent is left
                   as a dangling workspace (recoverable via sync-gateway endpoint).
    """
    gw = gateway if gateway is not None else _default_gateway
    agent_id = req.id.strip().lower().replace(" ", "-")

    # Normalise the model ID to the slash-prefixed form OpenClaw requires.
    # Mutating the request in place so every downstream read (gateway call,
    # DB write, ledger snapshot) sees the canonical value.
    req.model = normalize_model_id(req.model)

    existing = await pool.fetchrow(
        "SELECT id, status FROM agents WHERE id = $1", agent_id,
    )
    if existing and existing["status"] == "active":
        raise ValueError(f"Agent '{agent_id}' already exists")

    if existing and existing["status"] == "deleted":
        # child rows first (FK order: agent_files, agent_contexts, agent_gateway_bindings,
        # agent_configs → agents). agent_configs must be cleared so the new v1 snapshot
        # is not silently skipped by the ON CONFLICT DO NOTHING on re-provision.
        await pool.execute("DELETE FROM agent_files WHERE agent_id = $1", agent_id)
        await pool.execute("DELETE FROM agent_contexts WHERE agent_id = $1", agent_id)
        await pool.execute("DELETE FROM agent_gateway_bindings WHERE agent_id = $1", agent_id)
        await pool.execute("DELETE FROM agent_configs WHERE agent_id = $1", agent_id)
        await pool.execute("DELETE FROM agents WHERE id = $1", agent_id)
        logger.info("Cleaned up soft-deleted agent %s before re-creation", agent_id)

    # Build workspace file map. base_system_prompt and soul are API convenience
    # aliases that translate to AGENTS.md and SOUL.md respectively.
    files_to_write: dict[str, str] = {}
    if req.base_system_prompt:
        files_to_write["AGENTS.md"] = req.base_system_prompt
    if req.soul:
        files_to_write["SOUL.md"] = req.soul
    for filename, content in req.files.items():
        if content.strip():
            files_to_write[filename] = content

    # --- Gateway provisioning ---
    logger.info("Provisioning gateway agent %s", agent_id)
    try:
        gw_result = await gw.create_agent(
            name=agent_id,
            workspace=_workspace_path(agent_id),
            emoji=req.emoji,
        )
        gw_agent_id: str = gw_result.get("agentId", agent_id)
        workspace: str = gw_result.get("workspace", _workspace_path(agent_id))
        logger.info("Gateway agents.create OK: %s (workspace=%s)", gw_agent_id, workspace)
    except GatewayError as exc:
        if "already exists" not in str(exc):
            raise
        # Dangling gateway agent from a previous failed provision — reuse it
        logger.warning("Gateway agent %s already exists, reusing (previous provision failed?)", agent_id)
        gw_agent_id = agent_id
        workspace = _workspace_path(agent_id)

    # Track whether every post-create gateway call succeeded.
    # agents.create itself re-raises on failure (above); subsequent calls
    # are soft-fail so provisioning completes even when the gateway is
    # partially unavailable. gateway_sync_ok drives whether we record
    # the agent as synced (v1) or as needing a sync pass (NULL).
    gateway_sync_ok = True

    try:
        await gw.update_agent(gw_agent_id, model=req.model)
    except Exception as exc:
        logger.warning("agents.update (model) failed for %s: %s", gw_agent_id, exc)
        gateway_sync_ok = False

    if files_to_write:
        results = await asyncio.gather(
            *[gw.set_agent_file(gw_agent_id, fn, content)
              for fn, content in files_to_write.items()],
            return_exceptions=True,
        )
        for fn, result in zip(files_to_write.keys(), results):
            if isinstance(result, Exception):
                logger.warning("agents.files.set(%s) failed for %s: %s", fn, gw_agent_id, result)
                gateway_sync_ok = False
            else:
                logger.info("Wrote %s for agent %s (%d chars)", fn, gw_agent_id, len(files_to_write[fn]))

    # Resolve effective tool allow and deny lists.
    # Profiles are expanded first; cancellation removes tools present in both lists.
    # Both final lists are sent to OpenClaw in a single agents.update call.
    effective_allow, effective_deny = await resolve_agent_tools(
        pool, agent_id, req.template_id,
        list(req.tools_allow), list(req.tools_deny or []),
    )

    if effective_allow or effective_deny:
        try:
            await gw.set_agent_tools(gw_agent_id, effective_allow, effective_deny)
        except Exception as exc:
            logger.warning("Failed to set tools for %s: %s", gw_agent_id, exc)
            gateway_sync_ok = False

    # --- Persist to DB (event-first, then projection) ---
    # Build once; reused for both the ledger payload and the creation_params snapshot.
    agent_snapshot = {
        "agent_id": agent_id,
        "name": req.name,
        "type": req.type,
        "scope": req.scope,
        "model": req.model,
        "emoji": req.emoji,
        "tools_allow": req.tools_allow,
        "tools_deny": req.tools_deny,
        "memory_enabled": req.memory_enabled,
        "max_tokens": req.max_tokens,
        "gateway_agent_id": gw_agent_id,
        "workspace": workspace,
        "files_written": list(files_to_write.keys()),
    }

    file_hashes: dict[str, tuple[str, int]] = {
        fn: (hashlib.md5(c.encode()).hexdigest(), len(c.encode()))
        for fn, c in files_to_write.items()
    }

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=AGENT_PROVISIONED,
                payload=agent_snapshot,
                actor_id=actor_id,
            )
            # gateway_synced_version=1 only when every post-create gateway call
            # succeeded; NULL means the agent needs a sync pass via sync-gateway.
            synced_v = 1 if gateway_sync_ok else None
            agent_actor_id = await conn.fetchval(
                """INSERT INTO agents
                       (id, name, type, scope, model, emoji,
                        tools_allow, tools_deny, memory_enabled, max_tokens, notes,
                        created_by, gateway_agent_id, workspace,
                        config_version, gateway_synced_version, gateway_sync_attempted_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,now())
                   RETURNING actor_id""",
                agent_id, req.name, req.type, req.scope,
                req.model, req.emoji,
                req.tools_allow, req.tools_deny, req.memory_enabled, req.max_tokens,
                req.notes, actor_id, gw_agent_id, workspace,
                1, synced_v,
            )
            await enroll_agent_principal(
                conn,
                actor_id=agent_actor_id,
                agent_id=agent_id,
                display_name=req.name,
                gateway_agent_id=gw_agent_id,
                workspace=workspace,
                model=req.model,
                agent_type=req.type,
                scope=req.scope,
                emoji=req.emoji,
                tools_allow=req.tools_allow,
                tools_deny=list(req.tools_deny or []),
                memory_enabled=req.memory_enabled,
                created_by=actor_id,
                max_tokens=req.max_tokens,
                notes=req.notes or "",
                synced_config_version=synced_v,
            )
            for filename, content in files_to_write.items():
                ch, sz = file_hashes[filename]
                await conn.execute(
                    """INSERT INTO agent_files
                           (agent_id, filename, content, written_by, content_hash, file_size_bytes)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       ON CONFLICT (agent_id, filename)
                       DO UPDATE SET content = $3, written_at = now(), content_hash = $5, file_size_bytes = $6""",
                    agent_id, filename, content, actor_id, ch, sz,
                )
                await conn.execute(
                    """INSERT INTO agent_file_history
                           (agent_id, filename, content_hash, file_size_bytes, config_version, written_by)
                       VALUES ($1, $2, $3, $4, 1, $5)""",
                    agent_id, filename, ch, sz, actor_id,
                )

    if gateway_sync_ok:
        logger.info("Agent %s provisioned and fully synced (gw_id=%s, files=%s)", agent_id, gw_agent_id, list(files_to_write.keys()))
    else:
        logger.warning(
            "Agent %s provisioned but gateway sync incomplete (gw_id=%s) — "
            "use sync-gateway endpoint to push pending changes",
            agent_id, gw_agent_id,
        )

    return {**agent_snapshot, "notes": req.notes, "config_version": 1}
