"""System/lifecycle message relay — posts to goal pages and orchestrator feed."""
import logging

from ._binding import _INTERNAL_SIGNALS, _split_for_notion, _repair_stale_shca_binding

_log = logging.getLogger(__name__)


async def post_to_goal_page(goal_id: str, content: str, username: str = "System") -> None:
    """Post a system/lifecycle message to the goal's main Notion page.

    Targets the GO thread binding (actor_id IS NULL). Called by the generic
    relay_to_goal_channel dispatcher in base.py — do not call directly.
    """
    if not content or not content.strip():
        return

    try:
        from notion_client import AsyncClient
        from app.database import get_pool
        from app.channels.notion.secrets import resolve_token
    except ImportError as exc:
        _log.debug("Notion outbound unavailable: %s", exc)
        return

    try:
        pool = get_pool()
        integration = await pool.fetchrow(
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
        if not integration:
            return

        api_key = await resolve_token(
            integration["credentials_kind"],
            integration["credentials_data"],
            integration["credentials_ref"],
        )
        if not api_key:
            return

        page_row = await pool.fetchrow(
            """SELECT external_channel_id FROM platform_channel_bindings
               WHERE goal_id = $1 AND integration_id = $2
                 AND actor_id IS NULL AND archived_at IS NULL
               LIMIT 1""",
            goal_id, integration["id"],
        )
        if not page_row:
            _log.debug("Notion post_to_goal_page: no GO binding for goal=%s", goal_id)
            return

        page_id = page_row["external_channel_id"]
        body = f"[{username}] {content}" if username else content
        chunks = _split_for_notion(body)
        rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]

        notion = AsyncClient(auth=api_key)
        await notion.comments.create(parent={"page_id": page_id}, rich_text=rich_text)
        _log.info("Notion post_to_goal_page OK: goal=%s page=%s len=%d", goal_id, page_id, len(body))

    except Exception as exc:
        _log.warning("Notion post_to_goal_page error (goal=%s): %s", goal_id, exc)


async def post_to_thread_page(
    goal_id: str,
    thread_id: str,
    content: str,
    username: str = "System",
) -> None:
    """Post a system/lifecycle message to the Notion page bound to a specific thread.

    Called by the generic relay_to_thread_channel dispatcher in base.py — do not call directly.
    """
    if not content or not content.strip():
        return

    try:
        from notion_client import AsyncClient
        from app.channels.base import get_binding_by_thread
        from app.channels.notion.secrets import resolve_token
        from app.database import get_pool
    except ImportError as exc:
        _log.debug("Notion outbound unavailable: %s", exc)
        return

    try:
        binding = await get_binding_by_thread("notion", thread_id, goal_id)
        if not binding:
            binding = await get_binding_by_thread("notion_bot", thread_id, goal_id)
        if not binding:
            binding = await _repair_stale_shca_binding("notion", goal_id, thread_id)
            if not binding:
                binding = await _repair_stale_shca_binding("notion_bot", goal_id, thread_id)
        if not binding:
            _log.debug(
                "Notion post_to_thread_page: no binding for thread=%s goal=%s",
                thread_id[:12] if thread_id else "", goal_id,
            )
            return

        integration_id = binding.get("integration_id")
        if not integration_id:
            return

        creds = await get_pool().fetchrow(
            "SELECT credentials_kind, credentials_data, credentials_ref FROM platform_integrations WHERE id=$1 AND status='active'",
            integration_id,
        )
        if not creds:
            return

        api_key = await resolve_token(creds["credentials_kind"], creds["credentials_data"], creds["credentials_ref"])
        if not api_key:
            return

        page_id = binding.get("external_channel_id")
        if not page_id:
            return

        body = f"[{username}] {content}" if username else content
        chunks = _split_for_notion(body)
        rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]

        notion = AsyncClient(auth=api_key)
        await notion.comments.create(parent={"page_id": page_id}, rich_text=rich_text)
        _log.info(
            "Notion post_to_thread_page OK: goal=%s thread=%s page=%s len=%d",
            goal_id, thread_id[:12] if thread_id else "", page_id, len(body),
        )

    except Exception as exc:
        _log.warning(
            "Notion post_to_thread_page error (goal=%s thread=%s): %s",
            goal_id, thread_id[:12] if thread_id else "", exc,
        )


async def relay_orchestrator_message(goal_id: str, content: str) -> None:
    """Post an orchestrator channel message to the Notion orch subpage.

    Called from orchestrator.py after each message insert. The orch subpage
    is write-only from Notion's side — no inbound binding exists for it.
    """
    if not content or not content.strip():
        return
    if content.strip() in _INTERNAL_SIGNALS:
        return

    try:
        import json as _json
        from notion_client import AsyncClient
        from app.channels.notion.secrets import resolve_token
        from app.database import get_pool
    except ImportError as exc:
        _log.debug("Notion outbound unavailable: %s", exc)
        return

    try:
        pool = get_pool()

        integration = await pool.fetchrow(
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
        if not integration:
            return

        api_key = await resolve_token(
            integration["credentials_kind"],
            integration["credentials_data"],
            integration["credentials_ref"],
        )
        if not api_key:
            return

        gpi = await pool.fetchrow(
            "SELECT config FROM goal_platform_integrations WHERE goal_id=$1 AND integration_id=$2",
            goal_id, integration["id"],
        )
        if not gpi:
            return

        cfg = gpi["config"] or {}
        if isinstance(cfg, str):
            cfg = _json.loads(cfg)
        orch_page_id = cfg.get("orch_subpage_id")
        if not orch_page_id:
            return

        chunks = _split_for_notion(content)
        rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]

        notion = AsyncClient(auth=api_key)
        await notion.comments.create(
            parent={"page_id": orch_page_id},
            rich_text=rich_text,
        )
        _log.info("Notion orch relay OK: goal=%s page=%s len=%d", goal_id, orch_page_id, len(content))

    except Exception as exc:
        _log.warning("Notion orch relay error (goal=%s): %s", goal_id, exc)
