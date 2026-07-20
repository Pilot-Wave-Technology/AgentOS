"""Agent and human message relay — posts to SHCA Notion pages as comments."""
import logging

from ._binding import _INTERNAL_SIGNALS, _split_for_notion, _repair_stale_shca_binding, _lazy_bind_shca_thread

_log = logging.getLogger(__name__)


async def relay_agent_message(
    goal_id: str,
    thread_id: str,
    content: str,
    msg_id: str | None = None,
) -> None:
    """Post an agent reply as a Notion comment on the bound page. Fire-and-forget.

    msg_id: the messages.id for this agent reply — stamps external_message_id
    directly, avoiding a racy retrospective lookup.
    """
    if not content or not content.strip():
        return
    if content.strip() in _INTERNAL_SIGNALS:
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
            await _lazy_bind_shca_thread(goal_id, thread_id)
            binding = await get_binding_by_thread("notion", thread_id, goal_id)
            if not binding:
                binding = await get_binding_by_thread("notion_bot", thread_id, goal_id)
        if not binding:
            _log.info("Notion relay: no binding for thread=%s goal=%s", thread_id[:12] if thread_id else "", goal_id)
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
            _log.warning("Notion relay: could not resolve API key for goal=%s", goal_id)
            return

        page_id = binding.get("external_channel_id")
        if not page_id:
            _log.warning("Notion relay: binding has no external_channel_id for thread=%s", thread_id[:12])
            return

        chunks = _split_for_notion(content)
        rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]

        notion = AsyncClient(auth=api_key)
        result = await notion.comments.create(
            parent={"page_id": page_id},
            rich_text=rich_text,
        )
        external_message_id = result.get("id", "")

        if external_message_id and msg_id:
            await get_pool().execute(
                "UPDATE messages SET external_message_id=$1 WHERE id=$2",
                external_message_id, msg_id,
            )

        _log.info("Notion relay OK: goal=%s thread=%s page=%s len=%d", goal_id, thread_id[:12], page_id, len(content))

    except Exception as exc:
        _log.warning("Notion relay error (goal=%s thread=%s): %s", goal_id, thread_id[:12], exc, exc_info=True)


async def relay_human_message(
    goal_id: str,
    thread_id: str,
    content: str,
    sender_display_name: str = "",
) -> None:
    """Post a UI-originated human message as a Notion comment on the bound page.

    Mirrors what the user typed in the platform chat to Notion so readers on
    the Notion side see both sides of the conversation.
    Echo-safe: comments posted by our bot carry author.type='bot', which the
    inbound handler drops before routing back.
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
            await _lazy_bind_shca_thread(goal_id, thread_id)
            binding = await get_binding_by_thread("notion", thread_id, goal_id)
            if not binding:
                binding = await get_binding_by_thread("notion_bot", thread_id, goal_id)
        if not binding:
            _log.debug("Notion human relay: no binding for thread=%s goal=%s", thread_id[:12] if thread_id else "", goal_id)
            return

        integration_id = binding.get("integration_id")
        if not integration_id:
            _log.debug("Notion human relay: binding missing integration_id thread=%s", thread_id[:12] if thread_id else "")
            return

        creds = await get_pool().fetchrow(
            "SELECT credentials_kind, credentials_data, credentials_ref FROM platform_integrations WHERE id=$1 AND status='active'",
            integration_id,
        )
        if not creds:
            _log.debug("Notion human relay: no active credentials integration=%s", integration_id)
            return

        api_key = await resolve_token(creds["credentials_kind"], creds["credentials_data"], creds["credentials_ref"])
        if not api_key:
            _log.warning("Notion human relay: could not resolve API key for goal=%s", goal_id)
            return

        page_id = binding.get("external_channel_id")
        if not page_id:
            _log.warning("Notion human relay: binding has no external_channel_id thread=%s", thread_id[:12] if thread_id else "")
            return

        body = f"[{sender_display_name}]: {content}" if sender_display_name else content
        chunks = _split_for_notion(body)
        rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]

        notion = AsyncClient(auth=api_key)
        await notion.comments.create(parent={"page_id": page_id}, rich_text=rich_text)
        _log.info("Notion human relay OK: goal=%s thread=%s page=%s len=%d", goal_id, thread_id[:12] if thread_id else "", page_id, len(content))

    except Exception as exc:
        _log.warning("Notion human relay error (goal=%s thread=%s): %s", goal_id, thread_id[:12] if thread_id else "", exc, exc_info=True)


async def relay_file_upload(
    goal_id: str,
    uploader_uid: str,
    uploader_display_name: str,
    filename: str,
    file_size_kb: int,
    file_ext: str,
    sender_agent: str = "",
) -> None:
    """Post a file-upload notification as a Notion comment on the relevant page."""
    try:
        from app.database import get_pool
        pool = get_pool()

        if sender_agent == "go":
            thread_id = await pool.fetchval("SELECT go_thread_id FROM goals WHERE id=$1", goal_id)
        else:
            thread_id = await pool.fetchval(
                """SELECT t.id FROM threads t
                   JOIN agents a ON a.id = t.agent_id
                   WHERE t.goal_id=$1 AND t.created_by=$2 AND a.type='SHCA'
                   ORDER BY t.created_at DESC LIMIT 1""",
                goal_id, uploader_uid,
            )

        if not thread_id:
            _log.debug("Notion file relay: no thread found goal=%s user=%s sender_agent=%s", goal_id, uploader_uid, sender_agent)
            return

        comment = f"\U0001f4ce [{uploader_display_name}] uploaded {filename} ({file_ext.upper()}, {file_size_kb} KB)"
        await relay_human_message(goal_id, str(thread_id), comment)
        _log.info("Notion file relay OK: goal=%s user=%s file=%s", goal_id, uploader_uid, filename)

    except Exception as exc:
        _log.warning("Notion file relay error (goal=%s user=%s): %s", goal_id, uploader_uid, exc)
