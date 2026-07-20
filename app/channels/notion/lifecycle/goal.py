"""Goal, orchestrator, member, and resource lifecycle event handlers."""
import logging

from ._config import _get_notion_config

_log = logging.getLogger(__name__)


async def _on_orchestrator_message(goal_id: str, payload: dict) -> None:
    """Append an orchestrator message as a row in the Orchestrator timeline DB."""
    content  = payload.get("content", "")
    username = payload.get("username", "Orchestrator")
    if not content:
        return

    nc = await _get_notion_config(goal_id)
    if nc:
        access_token, cfg, _integration_id = nc
        orch_db_id = cfg.get("orch_db_id", "")
        if orch_db_id:
            try:
                from notion_client import AsyncClient
                from app.channels.notion.pages import append_orchestrator_event
                notion = AsyncClient(auth=access_token)
                await append_orchestrator_event(
                    orch_db_id, content[:500],
                    "orchestrator.message", "Agent", notion,
                )
                return
            except Exception as exc:
                _log.warning("Notion orchestrator.message: orch event failed goal=%s: %s", goal_id, exc)

    # Fallback for old integrations that only have orch_subpage_id.
    from app.channels.notion.outbound import relay_orchestrator_message
    await relay_orchestrator_message(goal_id, content)


async def _on_member_joined(goal_id: str, payload: dict) -> None:
    """Append a member-joined row to the Orchestrator DB."""
    nc = await _get_notion_config(goal_id)
    if not nc:
        return
    access_token, cfg, _integration_id = nc
    orch_db_id = cfg.get("orch_db_id", "")

    display_name = payload.get("display_name") or payload.get("uid", "Unknown")
    _log.info("Notion lifecycle: member.joined goal=%s user=%s", goal_id, display_name)

    if orch_db_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import append_orchestrator_event
            notion = AsyncClient(auth=access_token)
            await append_orchestrator_event(
                orch_db_id,
                f"👤 {display_name} joined the goal",
                "member.joined", "System", notion,
            )
        except Exception as exc:
            _log.warning("Notion member.joined: orch event failed goal=%s: %s", goal_id, exc)
    else:
        from app.channels.notion.outbound import post_to_goal_page
        await post_to_goal_page(goal_id, f"👤 *{display_name}* joined the goal.", username="System")


async def _on_member_left(goal_id: str, payload: dict) -> None:
    """Append a member-left row to the Orchestrator DB."""
    nc = await _get_notion_config(goal_id)
    if not nc:
        return
    access_token, cfg, _integration_id = nc
    orch_db_id = cfg.get("orch_db_id", "")

    display_name = payload.get("display_name") or payload.get("uid", "Unknown")
    _log.info("Notion lifecycle: member.left goal=%s user=%s", goal_id, display_name)

    if orch_db_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import append_orchestrator_event
            notion = AsyncClient(auth=access_token)
            await append_orchestrator_event(
                orch_db_id,
                f"👤 {display_name} left the goal",
                "member.left", "System", notion,
            )
        except Exception as exc:
            _log.warning("Notion member.left: orch event failed goal=%s: %s", goal_id, exc)
    else:
        from app.channels.notion.outbound import post_to_goal_page
        await post_to_goal_page(goal_id, f"👤 *{display_name}* left the goal.", username="System")


async def _on_resource_registered(goal_id: str, payload: dict) -> None:
    """Append a resource row to the Resources Feed database."""
    nc = await _get_notion_config(goal_id)
    if not nc:
        return
    access_token, cfg, _integration_id = nc
    resources_feed_db_id = cfg.get("resources_feed_db_id", "")
    if not resources_feed_db_id:
        return

    filename     = payload.get("filename", "")
    file_type    = payload.get("file_type", "")
    file_size_kb = payload.get("file_size_kb", 0)
    uploader_uid = payload.get("uploader_uid", "")
    resource_id  = payload.get("resource_id", "")

    _log.info("Notion lifecycle: resource.registered goal=%s file=%s", goal_id, filename)

    try:
        import datetime
        from notion_client import AsyncClient
        from app.database import get_pool
        notion = AsyncClient(auth=access_token)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        uploader_display = uploader_uid
        if uploader_uid:
            uploader_display = await get_pool().fetchval(
                "SELECT display_name FROM users WHERE uid = $1", uploader_uid,
            ) or uploader_uid
        await notion.pages.create(
            parent={"type": "database_id", "database_id": resources_feed_db_id},
            properties={
                "Name":        {"title": [{"type": "text", "text": {"content": filename[:2000]}}]},
                "File Type":   {"select": {"name": file_type or "other"}},
                "Size KB":     {"number": int(file_size_kb)},
                "Uploaded By": {"rich_text": [{"type": "text", "text": {"content": uploader_display[:2000]}}]},
                "Resource ID": {"rich_text": [{"type": "text", "text": {"content": str(resource_id)[:2000]}}]},
                "Added":       {"date": {"start": ts}},
            },
        )
    except Exception as exc:
        _log.warning("Notion resource.registered: feed row failed goal=%s file=%s: %s", goal_id, filename, exc)


async def _on_goal_completed(goal_id: str, payload: dict) -> None:
    """Append a goal-completed event to the Orchestrator DB and post to the goal page."""
    from app.channels.notion.outbound import post_to_goal_page

    goal_name = payload.get("goal_name", f"`{goal_id[:8]}`")
    _log.info("Notion lifecycle: goal.completed goal=%s name=%r", goal_id, goal_name)

    nc = await _get_notion_config(goal_id)
    if nc:
        access_token, cfg, _integration_id = nc
        orch_db_id = cfg.get("orch_db_id", "")
        if orch_db_id:
            try:
                from notion_client import AsyncClient
                from app.channels.notion.pages import append_orchestrator_event
                notion = AsyncClient(auth=access_token)
                await append_orchestrator_event(
                    orch_db_id,
                    f"✅ Goal {goal_name} completed",
                    "goal.completed", "System", notion,
                )
            except Exception as exc:
                _log.warning("Notion goal.completed: orch event failed goal=%s: %s", goal_id, exc)

    await post_to_goal_page(goal_id, f"✅ Goal *{goal_name}* completed.", username="System")


async def _on_goal_deleted(goal_id: str, payload: dict) -> None:
    """Post a deletion notice to the goal page and all active story-page bindings."""
    from app.channels.notion.outbound import post_to_goal_page, post_to_thread_page
    from app.database import get_pool

    goal_name = payload.get("goal_name", f"`{goal_id[:8]}`")
    _log.info("Notion lifecycle: goal.deleted — posting notices for goal=%s name=%r", goal_id, goal_name)
    msg = f"🗑 Goal *{goal_name}* has been deleted. This page will no longer receive updates."

    await post_to_goal_page(goal_id, msg, username="System")

    pool = get_pool()
    story_threads = await pool.fetch(
        """SELECT DISTINCT pcb.thread_id
           FROM platform_channel_bindings pcb
           JOIN platform_integrations pi ON pi.id = pcb.integration_id
           JOIN goal_platform_integrations gpi ON gpi.integration_id = pcb.integration_id
                                              AND gpi.goal_id = pcb.goal_id
           WHERE pcb.goal_id    = $1
             AND pcb.actor_id   IS NOT NULL
             AND pcb.archived_at IS NULL
             AND pi.platform    IN ('notion', 'notion_bot')
             AND pi.status      = 'active'
             AND gpi.enabled    = true""",
        goal_id,
    )
    _log.info("Notion lifecycle: goal.deleted notifying %d story pages goal=%s", len(story_threads), goal_id)
    for row in story_threads:
        try:
            await post_to_thread_page(goal_id, str(row["thread_id"]), msg, username="System")
        except Exception as exc:
            _log.warning(
                "Notion lifecycle: goal.deleted notice failed goal=%s thread=%s: %s",
                goal_id, str(row["thread_id"])[:12], exc,
            )
