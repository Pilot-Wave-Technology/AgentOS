"""NotionProvisioner and NotionBotProvisioner — ChannelProvisioner implementations."""
import json as _json
import logging

from app.channels.base import ChannelProvisioner
from ._helpers import _parse_notion_page_id
from ._goal import _do_provision_goal_structure

_log = logging.getLogger(__name__)


async def _provision_member_page(goal_id: str, uid: str, pool) -> None:
    """Look up the active Notion integration for a goal and provision a member page."""
    from app.channels.notion.provisioner._member import _do_provision_member_subpage
    from app.channels.notion.secrets import resolve_token

    row = await pool.fetchrow(
        """SELECT pi.id, pi.credentials_kind, pi.credentials_data, pi.credentials_ref
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
        return
    access_token = await resolve_token(
        row["credentials_kind"], row["credentials_data"], row["credentials_ref"],
    )
    if not access_token:
        return
    await _do_provision_member_subpage(goal_id, uid, str(row["id"]), access_token, pool)


class NotionProvisioner(ChannelProvisioner):
    """OAuth Notion integration provisioner."""

    def parse_channel_id(self, raw: str) -> str:
        return _parse_notion_page_id(raw)

    def is_auto_provisioned(self, config: dict) -> bool:
        return bool(config.get("parent_database_id"))

    async def provision_goal(self, goal_id: str, integration_id: str, pool) -> str | None:
        integration = await pool.fetchrow(
            "SELECT credentials_data, config FROM platform_integrations WHERE id = $1",
            integration_id,
        )
        if not integration:
            return "Integration not found"
        raw_cfg = integration["config"] or {}
        cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        parent_database_id = cfg.get("parent_database_id", "")
        access_token = integration["credentials_data"] or ""
        if not parent_database_id or not access_token:
            _log.info("Notion: no database/token — skipping page creation goal=%s", goal_id)
            return None
        return await _do_provision_goal_structure(
            goal_id, integration_id, access_token, parent_database_id, pool,
        )

    async def provision_member(self, goal_id: str, uid: str, pool) -> None:
        """Create a member page under the goal and bind it to the user's SHCA thread."""
        try:
            await _provision_member_page(goal_id, uid, pool)
        except Exception as exc:
            _log.warning("Notion provision_member: member page failed uid=%s: %s", uid, exc)


class NotionBotProvisioner(NotionProvisioner):
    """Internal-token Notion integration provisioner.

    Identical to NotionProvisioner except:
    - provision_goal falls back to the OAuth integration's parent_database_id
    - provision_member uses the notion_bot platform row
    """

    async def provision_goal(self, goal_id: str, integration_id: str, pool) -> str | None:
        integration = await pool.fetchrow(
            "SELECT credentials_data, config FROM platform_integrations WHERE id = $1",
            integration_id,
        )
        if not integration:
            return "Integration not found"
        raw_cfg = integration["config"] or {}
        cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        parent_database_id = cfg.get("parent_database_id", "")
        access_token = integration["credentials_data"] or ""

        if not parent_database_id:
            notion_row = await pool.fetchrow(
                "SELECT config FROM platform_integrations WHERE platform = 'notion' AND status = 'active'",
            )
            if notion_row:
                raw_notion = notion_row["config"] or {}
                notion_cfg = _json.loads(raw_notion) if isinstance(raw_notion, str) else (raw_notion or {})
                parent_database_id = notion_cfg.get("parent_database_id", "")

        if not parent_database_id or not access_token:
            _log.info("NotionBot: no database/token — skipping page creation goal=%s", goal_id)
            return None
        return await _do_provision_goal_structure(
            goal_id, integration_id, access_token, parent_database_id, pool,
        )

    async def provision_member(self, goal_id: str, uid: str, pool) -> None:
        """Create a member page under the goal and bind it to the user's SHCA thread."""
        try:
            await _provision_member_page(goal_id, uid, pool)
        except Exception as exc:
            _log.warning("NotionBot provision_member: member page failed uid=%s: %s", uid, exc)
