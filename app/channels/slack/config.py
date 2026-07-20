"""Slack config helpers — workspace credentials and per-goal channel config
from platform_integrations / goal_platform_integrations (generic channel tables).

Does NOT import from app.slack.* — fully self-contained in the new channel system.
"""
import json
import logging
from typing import Optional

_log = logging.getLogger(__name__)


def _decode(raw) -> dict:
    """Decode a JSONB or TEXT column value to a plain dict.

    asyncpg returns JSONB columns as Python dicts, but TEXT columns containing
    JSON as strings. Both forms appear in practice across branch databases where
    the column type differs.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


async def save_app_credentials(
    client_id: str, client_secret: str, signing_secret: str = "",
) -> None:
    """Store Slack app credentials in platform_integrations.config.

    Stores client_id, client_secret, and signing_secret so the entire Slack
    app setup can be done from the admin UI without touching env vars.
    Credentials are kept in config JSONB so they are never overwritten by the
    per-workspace bot tokens stored in credentials_json.
    """
    from app.database import get_pool
    import json as _json
    pool = get_pool()
    existing = await pool.fetchrow(
        "SELECT id, config FROM platform_integrations WHERE platform = 'slack' LIMIT 1",
    )
    if existing:
        raw = existing["config"] or {}
        cfg = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        cfg["client_id"] = client_id
        if client_secret:
            cfg["client_secret"] = client_secret
        if signing_secret:
            cfg["signing_secret"] = signing_secret
        await pool.execute(
            "UPDATE platform_integrations SET config = $1::jsonb, updated_at = now() WHERE id = $2",
            _json.dumps(cfg), existing["id"],
        )
        _log.info("Slack app credentials updated (integration id=%s)", existing["id"])
    else:
        cfg = {
            "client_id": client_id,
            "client_secret": client_secret,
            "signing_secret": signing_secret,
        }
        await pool.execute(
            "INSERT INTO platform_integrations (platform, credentials_kind, status, config) "
            "VALUES ('slack', 'oauth', 'pending_auth', $1::jsonb)",
            _json.dumps(cfg),
        )
        _log.info("Slack platform_integrations row created with app credentials")


async def get_app_credentials() -> dict:
    """Return Slack app credentials stored in platform_integrations.config.

    Returns: {client_id, has_secret, has_signing_secret, status}.
    Never returns raw secrets.
    """
    from app.database import get_pool
    import json as _json
    row = await get_pool().fetchrow(
        "SELECT id, config, status FROM platform_integrations WHERE platform = 'slack' LIMIT 1",
    )
    if not row:
        return {}
    raw = row["config"] or {}
    cfg = _json.loads(raw) if isinstance(raw, str) else dict(raw)
    return {
        "client_id": cfg.get("client_id", ""),
        "has_secret": bool(cfg.get("client_secret")),
        "has_signing_secret": bool(cfg.get("signing_secret")),
        "status": row["status"] or "",
    }


async def get_signing_secret() -> str:
    """Return the Slack signing secret from DB config.

    Returns empty string if not configured — caller falls back to env var.
    """
    from app.database import get_pool
    import json as _json
    row = await get_pool().fetchrow(
        "SELECT config FROM platform_integrations WHERE platform = 'slack' LIMIT 1",
    )
    if not row or not row["config"]:
        return ""
    raw = row["config"]
    cfg = _json.loads(raw) if isinstance(raw, str) else dict(raw)
    return cfg.get("signing_secret", "")


async def list_workspaces(pool=None) -> list[dict]:
    """Return all active Slack workspaces from platform_integrations.

    Returns list of {workspace_id, workspace_name, bot_user_id, status}.
    """
    from app.database import get_pool
    _pool = pool or get_pool()
    rows = await _pool.fetch(
        "SELECT credentials_json, status FROM platform_integrations "
        "WHERE platform = 'slack' AND status = 'active' ORDER BY created_at",
    )
    result = []
    for r in rows:
        creds = _decode(r["credentials_json"])
        if creds.get("workspace_id"):
            result.append({
                "workspace_id": creds.get("workspace_id", ""),
                "workspace_name": creds.get("workspace_name", ""),
                "bot_user_id": creds.get("bot_user_id", ""),
                "status": r["status"],
            })
    return result


async def get_integration_credentials(integration_id: str, pool) -> Optional[dict]:
    """Return Slack credentials dict for a platform_integrations row.

    Keys: bot_token, bot_user_id, workspace_id, workspace_name.
    """
    row = await pool.fetchrow(
        "SELECT credentials_json FROM platform_integrations WHERE id = $1 AND status = 'active'",
        integration_id,
    )
    if not row:
        return None
    return _decode(row["credentials_json"])


async def get_slack_config_for_goal(goal_id: str, pool) -> Optional[dict]:
    """Return combined Slack config for a goal.

    Returns: {integration_id, bot_token, bot_user_id, workspace_id,
              goal_channel_id, orch_channel_id, goal_channel_name, orch_channel_name}
    or None if Slack is not enabled for the goal.
    """
    row = await pool.fetchrow("""
        SELECT pi.id AS integration_id, pi.credentials_json, gpi.config AS goal_config
          FROM platform_integrations pi
          JOIN goal_platform_integrations gpi ON gpi.integration_id = pi.id
         WHERE gpi.goal_id = $1
           AND pi.platform = 'slack'
           AND pi.status = 'active'
           AND gpi.enabled = true
         LIMIT 1
    """, goal_id)
    if not row:
        return None
    creds = _decode(row["credentials_json"])
    config = _decode(row["goal_config"])
    return {
        "integration_id": str(row["integration_id"]),
        "enabled": True,
        "bot_token": creds.get("bot_token", ""),
        "bot_user_id": creds.get("bot_user_id", ""),
        "workspace_id": creds.get("workspace_id", ""),
        "goal_channel_id": config.get("goal_channel_id", ""),
        "orch_channel_id": config.get("orch_channel_id", ""),
        "goal_channel_name": config.get("goal_channel_name", ""),
        "orch_channel_name": config.get("orch_channel_name", ""),
    }


async def get_workspace_credentials(workspace_id: str, pool) -> Optional[dict]:
    """Return bot credentials for a Slack workspace by workspace_id.

    Returns: {integration_id, bot_token, bot_user_id, workspace_name} or None.
    """
    row = await pool.fetchrow("""
        SELECT id, credentials_json
          FROM platform_integrations
         WHERE platform = 'slack'
           AND status = 'active'
           AND credentials_json->>'workspace_id' = $1
         LIMIT 1
    """, workspace_id)
    if not row:
        return None
    creds = _decode(row["credentials_json"])
    return {
        "integration_id": str(row["id"]),
        "bot_token": creds.get("bot_token", ""),
        "bot_user_id": creds.get("bot_user_id", ""),
        "workspace_name": creds.get("workspace_name", ""),
    }


async def save_goal_channel_config(
    goal_id: str,
    integration_id: str,
    goal_channel_id: str,
    orch_channel_id: str,
    goal_channel_name: str = "",
    orch_channel_name: str = "",
    pool=None,
) -> None:
    """Persist goal/orch Slack channel IDs into goal_platform_integrations.config."""
    row = await pool.fetchrow(
        "SELECT config FROM goal_platform_integrations WHERE goal_id = $1 AND integration_id = $2",
        goal_id, integration_id,
    )
    config: dict = {}
    if row and row["config"]:
        raw = row["config"]
        config = _decode(raw)
    config.update({
        "goal_channel_id": goal_channel_id,
        "orch_channel_id": orch_channel_id,
        "goal_channel_name": goal_channel_name,
        "orch_channel_name": orch_channel_name,
    })
    await pool.execute(
        "UPDATE goal_platform_integrations SET config = $1::jsonb WHERE goal_id = $2 AND integration_id = $3",
        json.dumps(config), goal_id, integration_id,
    )
    _log.info(
        "Slack config saved: goal=%s goal_ch=%s orch_ch=%s",
        goal_id, goal_channel_id, orch_channel_id,
    )


async def upsert_workspace(
    workspace_id: str,
    workspace_name: str,
    bot_token: str,
    bot_user_id: str,
    app_id: str = "",
    installed_by: str = "",
    scopes: list | None = None,
    client_id: str = "",
    pool=None,
) -> str:
    """Store or update Slack workspace credentials in platform_integrations.

    client_id is stored alongside the token so the admin UI can detect when
    app credentials have been rotated and the bot token needs to be refreshed.

    Returns the integration_id (UUID string).
    """
    creds_data = {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "bot_token": bot_token,
        "bot_user_id": bot_user_id,
        "app_id": app_id,
        "installed_by": installed_by,
        "scopes": scopes or [],
        "installed_with_client_id": client_id,
    }
    existing = await pool.fetchrow(
        "SELECT id FROM platform_integrations WHERE platform='slack' AND credentials_json->>'workspace_id' = $1",
        workspace_id,
    )
    if existing:
        await pool.execute(
            "UPDATE platform_integrations SET credentials_json=$1, bot_external_id=$2, status='active', updated_at=now() WHERE id=$3",
            json.dumps(creds_data), bot_user_id, existing["id"],
        )
        _log.info("Updated Slack workspace integration: %s (%s)", workspace_name, workspace_id)
        return str(existing["id"])

    # platform_integrations has UNIQUE(platform), so there is at most ONE 'slack'
    # row. A prior row may already exist — created by save_app_credentials() with
    # only `config` (app client_id/secret) and no workspace_id — which the
    # workspace-id SELECT above does NOT match. A plain INSERT then hits
    # platform_integrations_platform_key. ON CONFLICT (platform) DO UPDATE merges
    # the workspace creds into that existing row, preserving `config` (only the
    # listed columns are touched).
    row = await pool.fetchrow("""
        INSERT INTO platform_integrations
            (platform, status, credentials_kind, credentials_json, bot_external_id)
        VALUES ('slack', 'active', 'oauth', $1, $2)
        ON CONFLICT (platform) DO UPDATE SET
            status='active',
            credentials_kind='oauth',
            credentials_json=EXCLUDED.credentials_json,
            bot_external_id=EXCLUDED.bot_external_id,
            updated_at=now()
        RETURNING id
    """, json.dumps(creds_data), bot_user_id)
    _log.info("Created/updated Slack workspace integration: %s (%s)", workspace_name, workspace_id)
    return str(row["id"])


async def get_goal_by_channel(workspace_id: str, channel_id: str, pool) -> Optional[dict]:
    """Find the goal whose main Slack channel matches channel_id.

    Matches the GO binding (actor_id IS NULL) in platform_channel_bindings.
    Returns {goal_id, bot_token, bot_user_id, workspace_id} or None.
    """
    row = await pool.fetchrow("""
        SELECT pcb.goal_id, pi.credentials_json
          FROM platform_channel_bindings pcb
          JOIN platform_integrations pi ON pi.id = pcb.integration_id
         WHERE pi.platform = 'slack'
           AND pi.credentials_json->>'workspace_id' = $1
           AND pcb.external_channel_id = $2
           AND pcb.actor_id IS NULL
           AND pcb.archived_at IS NULL
         LIMIT 1
    """, workspace_id, channel_id)
    if not row:
        return None
    creds = _decode(row["credentials_json"])
    return {
        "goal_id": str(row["goal_id"]),
        "bot_token": creds.get("bot_token", ""),
        "bot_user_id": creds.get("bot_user_id", ""),
        "workspace_id": workspace_id,
    }


async def get_binding_for_channel(channel_id: str, workspace_id: str, pool) -> Optional[dict]:
    """Look up any platform_channel_bindings row by external_channel_id.

    Used by /refresh to identify the agent (GO or SHCA) owning a channel.
    Returns {goal_id, thread_id, agent_id, actor_id, bot_token} or None.
    actor_id IS NULL → GO channel; actor_id IS NOT NULL → SHCA user channel.
    """
    row = await pool.fetchrow("""
        SELECT pcb.goal_id, pcb.thread_id, pcb.agent_id, pcb.actor_id,
               pi.credentials_json
          FROM platform_channel_bindings pcb
          JOIN platform_integrations pi ON pi.id = pcb.integration_id
         WHERE pi.platform = 'slack'
           AND pi.credentials_json->>'workspace_id' = $1
           AND pcb.external_channel_id = $2
           AND pcb.archived_at IS NULL
         LIMIT 1
    """, workspace_id, channel_id)
    if not row:
        return None
    creds = _decode(row["credentials_json"])
    return {
        "goal_id": str(row["goal_id"]),
        "thread_id": str(row["thread_id"]) if row["thread_id"] else "",
        "agent_id": str(row["agent_id"]) if row["agent_id"] else "",
        "actor_id": row["actor_id"],
        "bot_token": creds.get("bot_token", ""),
    }


async def get_workspace(team_id: str, pool) -> Optional[dict]:
    """Return workspace credentials by Slack team_id (= workspace_id).

    Returns {integration_id, bot_token, bot_user_id, workspace_name} or None.
    Alias for get_workspace_credentials with matching param name.
    """
    return await get_workspace_credentials(team_id, pool)


async def list_goals_for_workspace(workspace_id: str, pool) -> list[dict]:
    """List goals with their Slack channels for a workspace.

    Returns [{goal_id, goal_name, goal_channel_id}, ...] up to 50 entries.
    """
    rows = await pool.fetch("""
        SELECT g.id AS goal_id, g.name AS goal_name,
               gpi.config->>'goal_channel_id' AS goal_channel_id
          FROM goals g
          JOIN goal_platform_integrations gpi ON gpi.goal_id = g.id
          JOIN platform_integrations pi ON pi.id = gpi.integration_id
         WHERE pi.platform = 'slack'
           AND pi.credentials_json->>'workspace_id' = $1
           AND gpi.enabled = true
           AND g.status NOT IN ('deleted')
         ORDER BY g.name
         LIMIT 50
    """, workspace_id)
    return [dict(r) for r in rows]


async def find_goal_by_orch_channel(workspace_id: str, channel_id: str, pool) -> Optional[dict]:
    """Find the goal whose Slack orch_channel_id matches channel_id.

    Returns {goal_id, integration_id, bot_token, bot_user_id, goal_channel_id, orch_channel_id}
    or None.
    """
    rows = await pool.fetch("""
        SELECT gpi.goal_id, gpi.config, pi.credentials_json, pi.id AS integration_id
          FROM goal_platform_integrations gpi
          JOIN platform_integrations pi ON pi.id = gpi.integration_id
         WHERE pi.platform = 'slack'
           AND pi.status = 'active'
           AND gpi.enabled = true
           AND pi.credentials_json->>'workspace_id' = $1
    """, workspace_id)
    for row in rows:
        config = _decode(row["config"])
        if config.get("orch_channel_id") == channel_id:
            creds = _decode(row["credentials_json"])
            return {
                "goal_id": str(row["goal_id"]),
                "integration_id": str(row["integration_id"]),
                "bot_token": creds.get("bot_token", ""),
                "bot_user_id": creds.get("bot_user_id", ""),
                "goal_channel_id": config.get("goal_channel_id", ""),
                "orch_channel_id": channel_id,
            }
    return None