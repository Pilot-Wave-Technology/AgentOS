"""Shared config helper for all lifecycle event handlers."""
import json as _json
import logging

_log = logging.getLogger(__name__)


async def _get_notion_config(goal_id: str) -> tuple[str, dict, str] | None:
    """Return (access_token, gpi_config, integration_id) for the active Notion integration.

    Returns None if no active Notion integration is enabled for this goal.
    """
    from app.database import get_pool
    from app.channels.notion.secrets import resolve_token

    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT pi.id, pi.credentials_kind, pi.credentials_data, pi.credentials_ref,
                  gpi.config
           FROM platform_integrations pi
           JOIN goal_platform_integrations gpi ON gpi.integration_id = pi.id
           WHERE pi.platform IN ('notion', 'notion_bot')
             AND pi.status = 'active'
             AND gpi.goal_id = $1
             AND gpi.enabled = true
           LIMIT 1""",
        goal_id,
    )
    if not row:
        return None

    access_token = await resolve_token(
        row["credentials_kind"], row["credentials_data"], row["credentials_ref"],
    )
    if not access_token:
        return None

    raw_cfg = row["config"] or {}
    cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
    return access_token, cfg, str(row["id"])
