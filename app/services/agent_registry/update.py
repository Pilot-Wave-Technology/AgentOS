"""Agent update — apply a partial patch, sync to gateway, increment config_version."""
import asyncio
import hashlib
import logging
from typing import Any

from app.llm_models import normalize_model_id
from app.models.agent import AgentUpdateRequest
from app.openclaw import gateway_admin as _default_gateway
from app.ledger.journal import append_event
from app.ledger.schema import AGENT_UPDATED
from .helpers import resolve_agent_tools

logger = logging.getLogger(__name__)


async def update_agent(pool, agent_id: str, patch: AgentUpdateRequest, actor_id: str, *, gateway=None) -> dict[str, Any]:
    """Apply a partial update to an agent, sync changes to the gateway, and increment config_version.

    Only fields present (non-None) in patch are updated. Files dict is merged — provided
    keys overwrite existing; keys absent from patch are left unchanged.

    Raises:
        ValueError: Agent not found or is deleted.
        GatewayError: Gateway rejected an update call (non-file operations only).
    """
    gw = gateway if gateway is not None else _default_gateway
    row = await pool.fetchrow("SELECT * FROM agents WHERE id = $1 AND status = 'active'", agent_id)
    if not row:
        raise ValueError(f"Agent '{agent_id}' not found")
    current = dict(row)
    gw_id = current.get("gateway_agent_id") or agent_id

    # Normalise an unprefixed model ID before diffing (e.g. UI dropdown
    # submitted "gemini-2.5-flash" → "google/gemini-2.5-flash"). Without
    # this, the diff would mark every PATCH as a model change.
    if patch.model is not None:
        patch.model = normalize_model_id(patch.model)

    # Determine changed scalar fields (only provided non-None values that differ from current)
    scalar_fields = ("name", "type", "scope", "model", "emoji",
                     "tools_allow", "tools_deny",
                     "memory_enabled", "max_tokens", "notes")
    changed_scalars: dict[str, Any] = {}
    for field in scalar_fields:
        new_val = getattr(patch, field)
        if new_val is not None and new_val != current.get(field):
            changed_scalars[field] = new_val

    # Determine changed files
    changed_files: dict[str, str] = {}
    if patch.files:
        for filename, content in patch.files.items():
            if content.strip():
                changed_files[filename] = content
    # API convenience aliases: translate base_system_prompt → AGENTS.md, soul → SOUL.md
    if patch.base_system_prompt is not None:
        changed_files.setdefault("AGENTS.md", patch.base_system_prompt)
    if patch.soul is not None:
        changed_files.setdefault("SOUL.md", patch.soul)

    if not changed_scalars and not changed_files:
        return current  # nothing to do

    file_hashes: dict[str, tuple[str, int]] = {
        fn: (hashlib.md5(c.encode()).hexdigest(), len(c.encode()))
        for fn, c in changed_files.items()
    }

    # --- Gateway delta sync ---
    gw_scalar_updates: dict[str, Any] = {}
    if "model" in changed_scalars:
        gw_scalar_updates["model"] = changed_scalars["model"]
    if "name" in changed_scalars:
        gw_scalar_updates["name"] = changed_scalars["name"]
    if "emoji" in changed_scalars:
        gw_scalar_updates["emoji"] = changed_scalars["emoji"]

    if gw_scalar_updates:
        try:
            await gw.update_agent(gw_id, **gw_scalar_updates)
        except Exception as exc:
            logger.warning("agents.update failed for %s: %s", gw_id, exc)

    if changed_files:
        results = await asyncio.gather(
            *[gw.set_agent_file(gw_id, fn, content)
              for fn, content in changed_files.items()],
            return_exceptions=True,
        )
        for fn, result in zip(changed_files.keys(), results):
            if isinstance(result, Exception):
                logger.warning("agents.files.set(%s) failed for %s: %s", fn, gw_id, result)

    if "tools_allow" in changed_scalars:
        # Re-resolve via template profiles so the gateway always receives the full
        # expanded list, not just the delta. Cancellation removes tools in both lists.
        current_deny = list(changed_scalars.get("tools_deny") or current.get("tools_deny") or [])
        effective_allow, effective_deny = await resolve_agent_tools(
            pool, agent_id, current.get("template_id"),
            list(changed_scalars["tools_allow"]), current_deny,
        )
        try:
            await gw.set_agent_tools(gw_id, effective_allow, effective_deny)
        except Exception as exc:
            logger.warning("Failed to sync tools for %s: %s", gw_id, exc)

    # --- DB update (event-first, then projection) ---
    # $1..$N: scalar values, $N+1: new_version, $N+2: actor_id, $N+3: agent_id (WHERE)
    n = len(changed_scalars)
    parts = [f"{f} = ${i + 1}" for i, f in enumerate(changed_scalars)]
    parts.append(f"config_version = ${n + 1}")
    parts.append("updated_at = now()")
    parts.append(f"updated_by = ${n + 2}")
    parts.append("gateway_synced_version = NULL")
    parts.append("gateway_synced_at = NULL")
    set_clauses = ", ".join(parts)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the row so concurrent PATCH requests serialize and each reads
            # the correct config_version to increment. The outer pool.fetchrow
            # above was for the gateway delta-sync; this is the authoritative DB read.
            locked = await conn.fetchrow(
                "SELECT config_version FROM agents WHERE id = $1 AND status = 'active' FOR UPDATE",
                agent_id,
            )
            if not locked:
                raise ValueError(f"Agent '{agent_id}' not found or was deleted concurrently")
            new_version = (locked["config_version"] or 1) + 1
            values = list(changed_scalars.values()) + [new_version, actor_id, agent_id]
            await append_event(
                conn,
                event_type=AGENT_UPDATED,
                payload={
                    "agent_id": agent_id,
                    "changes": changed_scalars,
                    "files_updated": list(changed_files.keys()),
                    "config_version": new_version,
                },
                actor_id=actor_id,
            )
            await conn.execute(
                f"UPDATE agents SET {set_clauses} WHERE id = ${len(values)}",
                *values,
            )
            await conn.execute(
                """INSERT INTO agent_configs
                       (agent_id, config_version, model, type, scope, emoji,
                        tools_allow, tools_deny, memory_enabled, max_tokens, notes, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                   ON CONFLICT (agent_id, config_version) DO NOTHING""",
                agent_id, new_version,
                changed_scalars.get("model", current.get("model")),
                changed_scalars.get("type", current.get("type")),
                changed_scalars.get("scope", current.get("scope")),
                changed_scalars.get("emoji", current.get("emoji", "")),
                changed_scalars.get("tools_allow", current.get("tools_allow", [])),
                changed_scalars.get("tools_deny", current.get("tools_deny", [])),
                changed_scalars.get("memory_enabled", current.get("memory_enabled", False)),
                changed_scalars.get("max_tokens", current.get("max_tokens")),
                changed_scalars.get("notes", current.get("notes", "")),
                actor_id,
            )
            await conn.execute(
                """UPDATE agent_gateway_bindings
                   SET synced_config_version = NULL, last_sync_attempted_at = NULL
                   WHERE agent_id = $1""",
                agent_id,
            )
            if "name" in changed_scalars:
                await conn.execute(
                    "UPDATE actors SET display_name = $1 WHERE agent_id = $2",
                    changed_scalars["name"], agent_id,
                )
            for filename, content in changed_files.items():
                ch, sz = file_hashes[filename]
                await conn.execute(
                    """INSERT INTO agent_files
                           (agent_id, filename, content, written_by, content_hash, file_size_bytes)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       ON CONFLICT (agent_id, filename)
                       DO UPDATE SET content = $3, written_at = now(), written_by = $4,
                                     content_hash = $5, file_size_bytes = $6""",
                    agent_id, filename, content, actor_id, ch, sz,
                )
                await conn.execute(
                    """INSERT INTO agent_file_history
                           (agent_id, filename, content_hash, file_size_bytes, config_version, written_by)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    agent_id, filename, ch, sz, new_version, actor_id,
                )

    logger.info(
        "Agent %s updated (v%d): scalars=%s files=%s",
        agent_id, new_version, list(changed_scalars.keys()), list(changed_files.keys()),
    )

    updated = dict(current)
    updated.update(changed_scalars)
    updated["config_version"] = new_version
    updated["gateway_synced_version"] = None
    updated["gateway_synced_at"] = None
    return updated
