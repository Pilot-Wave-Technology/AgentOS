"""DEPRECATED — superseded by app/channels/slack/config.py (reads platform_* tables).
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Slack workspace + goal config CRUD — reads/writes slack_workspaces and slack_config tables."""
import logging
from typing import Optional

from app.database import get_pool

_log = logging.getLogger(__name__)


# ── Workspace CRUD ───────────────────────────────────────────────────────────

async def get_workspace(workspace_id: str) -> Optional[dict]:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM slack_workspaces WHERE workspace_id = $1", workspace_id,
    )
    return dict(row) if row else None


async def list_workspaces() -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT * FROM slack_workspaces WHERE status = 'active' ORDER BY workspace_name",
    )
    return [dict(r) for r in rows]


async def upsert_workspace(
    workspace_id: str,
    workspace_name: str,
    bot_token: str,
    bot_user_id: str,
    app_id: str = "",
    installed_by: str = "",
    scopes: list[str] | None = None,
) -> dict:
    pool = get_pool()
    row = await pool.fetchrow("""
        INSERT INTO slack_workspaces
            (workspace_id, workspace_name, bot_token, bot_user_id, app_id,
             installed_by, scopes, status, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', now())
        ON CONFLICT (workspace_id) DO UPDATE SET
            workspace_name = $2, bot_token = $3, bot_user_id = $4,
            app_id = $5, scopes = $7, status = 'active', updated_at = now()
        RETURNING *
    """, workspace_id, workspace_name, bot_token, bot_user_id, app_id,
        installed_by, scopes or [])
    _log.info("Upserted Slack workspace %s (%s)", workspace_id, workspace_name)
    return dict(row)


async def revoke_workspace(workspace_id: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE slack_workspaces SET status = 'revoked', updated_at = now() WHERE workspace_id = $1",
        workspace_id,
    )
    _log.info("Revoked Slack workspace %s", workspace_id)


# ── Goal config CRUD ─────────────────────────────────────────────────────────

async def get_goal_slack_config(goal_id: str) -> Optional[dict]:
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT sc.*, sw.workspace_name, sw.bot_token, sw.bot_user_id, sw.status AS ws_status
          FROM slack_config sc
          JOIN slack_workspaces sw ON sw.workspace_id = sc.workspace_id
         WHERE sc.goal_id = $1
    """, goal_id)
    return dict(row) if row else None


async def get_goal_by_channel(workspace_id: str, channel_id: str) -> Optional[dict]:
    """Find the goal whose MAIN channel matches channel_id."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT sc.*, sw.bot_token, sw.bot_user_id
          FROM slack_config sc
          JOIN slack_workspaces sw ON sw.workspace_id = sc.workspace_id
         WHERE sc.workspace_id = $1
           AND sc.goal_channel_id = $2
           AND sc.enabled = true
           AND sw.status = 'active'
    """, workspace_id, channel_id)
    return dict(row) if row else None


async def get_goal_by_orch_channel(workspace_id: str, channel_id: str) -> Optional[dict]:
    """Find the goal whose ORCHESTRATOR channel matches channel_id."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT sc.*, sw.bot_token, sw.bot_user_id
          FROM slack_config sc
          JOIN slack_workspaces sw ON sw.workspace_id = sc.workspace_id
         WHERE sc.workspace_id = $1
           AND sc.orch_channel_id = $2
           AND sc.enabled = true
           AND sw.status = 'active'
    """, workspace_id, channel_id)
    return dict(row) if row else None


async def upsert_goal_slack_config(
    goal_id: str,
    workspace_id: str,
    goal_channel_id: str,
    goal_channel_name: str = "",
    orch_channel_id: str = "",
    orch_channel_name: str = "",
) -> dict:
    pool = get_pool()
    row = await pool.fetchrow("""
        INSERT INTO slack_config
            (goal_id, workspace_id, goal_channel_id, goal_channel_name,
             orch_channel_id, orch_channel_name, enabled, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, true, now())
        ON CONFLICT (goal_id) DO UPDATE SET
            workspace_id = $2, goal_channel_id = $3, goal_channel_name = $4,
            orch_channel_id = $5, orch_channel_name = $6,
            enabled = true, updated_at = now()
        RETURNING *
    """, goal_id, workspace_id, goal_channel_id, goal_channel_name,
        orch_channel_id, orch_channel_name)
    _log.info(
        "Linked goal %s to Slack: goal=%s orch=%s",
        goal_id, goal_channel_name or goal_channel_id,
        orch_channel_name or orch_channel_id,
    )
    return dict(row)


async def disable_goal_slack(goal_id: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE slack_config SET enabled = false, updated_at = now() WHERE goal_id = $1",
        goal_id,
    )


async def list_goals_for_workspace(workspace_id: str) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT sc.*, g.name AS goal_name
          FROM slack_config sc
          JOIN goals g ON g.id = sc.goal_id
         WHERE sc.workspace_id = $1 AND sc.enabled = true AND g.status = 'active'
         ORDER BY g.name
    """, workspace_id)
    return [dict(r) for r in rows]
