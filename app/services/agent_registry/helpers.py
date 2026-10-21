"""Shared DB write helpers for agent provisioning.

These helpers are called inside open transactions — they must not acquire
their own connections or open new transactions.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def enroll_agent_principal(
    conn,
    *,
    actor_id,
    agent_id: str,
    display_name: str,
    gateway_agent_id: str,
    workspace: str,
    model: str,
    agent_type: str,
    scope: str,
    emoji: str,
    tools_allow: list,
    tools_deny: list,
    memory_enabled: bool,
    created_by: str,
    model_id: Optional[str] = None,
    max_tokens: Optional[int] = None,
    notes: str = "",
    synced_config_version: Optional[int] = None,
) -> None:
    """Write actors, agent_configs, and agent_gateway_bindings for a new agent.

    Must be called inside an open transaction on `conn` immediately after the
    agents INSERT that produced `actor_id`.  All three writes use ON CONFLICT
    DO NOTHING so the function is safe on idempotent re-provision paths
    (e.g. go_agent.py uses ON CONFLICT DO NOTHING on its agents INSERT — if the
    row already existed, actor_id comes back NULL, and the caller guards with
    `if actor_id:` before calling here).

    model_id: the resolved Model Registry FK companion to `model` (see
    `app.services.model_registry.dropdowns.resolve_model_id`) — optional
    and defaults to None so existing callers that haven't been updated to
    resolve it (e.g. go_agent.py) keep working unchanged; `agent_configs`
    just gets a NULL `model_id` in that case, same as before this column
    was ever written to.

    synced_config_version: pass the version number when the gateway was fully
    configured during this provision (sets last_synced_at = now()); pass None
    when sync is still pending so the drift-check query picks the agent up on
    its next pass.
    """
    await conn.execute(
        """INSERT INTO actors (id, type, agent_id, display_name)
           VALUES ($1, 'agent', $2, $3)
           ON CONFLICT (id) DO NOTHING""",
        actor_id, agent_id, display_name,
    )
    await conn.execute(
        """INSERT INTO agent_configs
               (agent_id, config_version, model, model_id, type, scope, emoji,
                tools_allow, tools_deny, memory_enabled, max_tokens, notes, created_by)
           VALUES ($1, 1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
           ON CONFLICT (agent_id, config_version) DO NOTHING""",
        agent_id, model, model_id, agent_type, scope, emoji,
        tools_allow, tools_deny, memory_enabled, max_tokens, notes, created_by,
    )
    await conn.execute(
        """INSERT INTO agent_gateway_bindings
               (agent_id, gateway_agent_id, workspace,
                synced_config_version, last_synced_at, last_sync_attempted_at)
           VALUES ($1, $2, $3, $4::integer,
                   CASE WHEN $4::integer IS NOT NULL THEN now() END,
                   now())
           ON CONFLICT (agent_id) DO NOTHING""",
        agent_id, gateway_agent_id, workspace, synced_config_version,
    )
    logger.debug(
        "enroll_agent_principal: actor=%s agent=%s type=%s synced_v=%s",
        actor_id, agent_id, agent_type, synced_config_version,
    )


async def resolve_agent_tools(
    pool,
    agent_id: str,
    template_id: Optional[str],
    instance_allow: list,
    deny_list: list,
) -> tuple[list, list]:
    """Resolve final tool allow/deny lists by merging template profiles with instance overrides.

    Called before every gateway agents.update(tools=...) — both on initial provision
    and on PATCH updates.  The logic:
      - If the agent has a template_id: fetch the template's profiles + base lists,
        merge with the instance overrides, then run resolve_tool_allowlist so profile
        expansions and cancellations are applied.
      - If no template but the instance has a deny list: apply cancellation to the
        direct allow list (removes tools present in both lists).
      - Otherwise: return the instance lists unchanged.

    Falls back to the raw instance lists if tool_registry resolution fails — gateway
    sync is soft-fail anyway, so a partial list beats a hard error.

    Returns (effective_allow, effective_deny).
    """
    effective_allow = list(instance_allow)
    effective_deny = list(deny_list)

    if template_id:
        from app.services.tool_registry import get_template_profiles, resolve_tool_allowlist
        try:
            template_profiles = await get_template_profiles(pool, template_id)
            tmpl = await pool.fetchrow(
                "SELECT tools_allow, tools_deny FROM templates WHERE id = $1", template_id,
            )
            if tmpl:
                merged_allow = list(tmpl["tools_allow"] or []) + list(instance_allow)
                merged_deny = list(tmpl["tools_deny"] or []) + list(deny_list)
                effective_allow, effective_deny = await resolve_tool_allowlist(
                    pool, template_profiles, merged_allow, merged_deny,
                )
                logger.info(
                    "resolve_agent_tools: agent=%s template=%s allow=%d deny=%d",
                    agent_id, template_id, len(effective_allow), len(effective_deny),
                )
        except Exception as exc:
            logger.warning(
                "resolve_agent_tools: failed for agent=%s template=%s, using instance list: %s",
                agent_id, template_id, exc,
            )
    elif effective_deny:
        from app.services.tool_registry import resolve_tool_allowlist
        try:
            effective_allow, effective_deny = await resolve_tool_allowlist(
                pool, [], effective_allow, effective_deny,
            )
        except Exception as exc:
            logger.warning(
                "resolve_agent_tools: cancellation failed for agent=%s: %s", agent_id, exc,
            )

    return effective_allow, effective_deny
