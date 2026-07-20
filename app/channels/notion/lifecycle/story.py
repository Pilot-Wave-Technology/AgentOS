"""Story lifecycle event handlers — story.created and story.state_changed."""
import logging

from ._config import _get_notion_config

_log = logging.getLogger(__name__)


_STORY_STATE_LABELS = {
    "proposed":    "📋 Proposed",
    "accepted":    "✅ Accepted",
    "in_progress": "🔄 In Progress",
    "blocked":     "⚠️ Blocked",
    "done":        "✅ Done",
    "cancelled":   "🚫 Cancelled",
    "rejected":    "❌ Rejected",
}


async def _on_story_created(goal_id: str, payload: dict) -> None:
    """Create a story card in the Stories Board and append an Orchestrator event."""
    nc = await _get_notion_config(goal_id)
    if not nc:
        return
    access_token, cfg, integration_id = nc
    stories_board_db_id = cfg.get("stories_board_db_id", "")
    orch_db_id          = cfg.get("orch_db_id", "")

    story_id    = payload.get("story_id", "")
    story_title = payload.get("story_title") or "New story"
    assigned_to = payload.get("assigned_to", "")
    state       = payload.get("state", "proposed")
    priority    = payload.get("priority", "")

    _log.info("Notion lifecycle: story.created goal=%s story=%s", goal_id, story_id)

    if stories_board_db_id and story_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import create_story_card
            notion = AsyncClient(auth=access_token)
            await create_story_card(
                stories_board_db_id, story_id, story_title, state,
                assigned_to, priority, notion, access_token,
            )
        except Exception as exc:
            _log.warning("Notion story.created: failed to create card story=%s: %s", story_id, exc)

    if orch_db_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import append_orchestrator_event
            notion = AsyncClient(auth=access_token)
            label = f"📌 Story created: {story_title}"
            if assigned_to:
                label += f" · {assigned_to}"
            await append_orchestrator_event(
                orch_db_id, label, "story.created", "Agent", notion,
            )
        except Exception as exc:
            _log.warning("Notion story.created: orch event failed goal=%s: %s", goal_id, exc)


async def _on_story_state_changed(
    goal_id: str,
    payload: dict,
    thread_id: str | None,
) -> None:
    """Update the story card Status in Notion, append an Orchestrator event, and
    notify the assigned user's member page of the state change.
    """
    story_id    = payload.get("story_id", "")
    story_title = payload.get("story_title") or f"`{story_id[:8]}`"
    old_state   = payload.get("old_state", "")
    new_state   = payload.get("new_state", "")
    state_label = _STORY_STATE_LABELS.get(new_state, new_state)

    _log.info(
        "Notion lifecycle: story.state_changed goal=%s story=%s %s→%s",
        goal_id, story_id[:8] if story_id else "", old_state, new_state,
    )

    nc = await _get_notion_config(goal_id)
    if not nc:
        return
    access_token, cfg, _integration_id = nc
    stories_board_db_id = cfg.get("stories_board_db_id", "")
    orch_db_id          = cfg.get("orch_db_id", "")

    # Update the story card Status property in the Stories Board.
    if stories_board_db_id and story_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import find_story_card, update_page_select_property, STORY_STATE_TO_NOTION
            notion = AsyncClient(auth=access_token)
            story_page_id = await find_story_card(stories_board_db_id, story_id, notion)
            if story_page_id:
                notion_label = STORY_STATE_TO_NOTION.get(new_state, new_state.title())
                await update_page_select_property(story_page_id, "Status", notion_label, notion)
                _log.info("Notion: updated story card status story=%s → %s", story_id, notion_label)
        except Exception as exc:
            _log.warning("Notion story.state_changed: card update failed story=%s: %s", story_id, exc)

    if orch_db_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import append_orchestrator_event
            notion = AsyncClient(auth=access_token)
            await append_orchestrator_event(
                orch_db_id,
                f"{story_title}: {old_state} → {new_state}",
                "story.state_changed", "System", notion,
            )
        except Exception as exc:
            _log.warning("Notion story.state_changed: orch event failed goal=%s: %s", goal_id, exc)

    # Build the notification message for the assigned user.
    if new_state in ("done", "cancelled", "rejected"):
        msg = f"Story *{story_title}* {state_label}."
    else:
        msg = f"Story status update: *{story_title}* → {state_label}"

    # If a thread_id was passed directly (legacy path), use it.
    if thread_id:
        from app.channels.notion.outbound import post_to_thread_page
        await post_to_thread_page(goal_id, thread_id, msg, username="System")
        return

    # Route notification to the assigned user's member page via their SHCA binding.
    if story_id:
        try:
            from app.database import get_pool
            pool = get_pool()
            assigned_uid = await pool.fetchval(
                "SELECT assigned_to FROM stories WHERE id = $1", story_id,
            )
            if assigned_uid:
                actor_id = await pool.fetchval(
                    "SELECT actor_id FROM users WHERE uid = $1", assigned_uid,
                )
                if actor_id:
                    binding = await pool.fetchrow(
                        """SELECT thread_id FROM platform_channel_bindings
                           WHERE goal_id = $1 AND actor_id = $2
                             AND archived_at IS NULL
                           LIMIT 1""",
                        goal_id, actor_id,
                    )
                    if binding and binding["thread_id"]:
                        from app.channels.notion.outbound import post_to_thread_page
                        await post_to_thread_page(
                            goal_id, str(binding["thread_id"]), msg, username="System",
                        )
                        _log.info(
                            "Notion: posted state-change to member page goal=%s story=%s user=%s",
                            goal_id, story_id[:8], assigned_uid,
                        )
                    else:
                        _log.debug(
                            "Notion story.state_changed: no member binding for user=%s goal=%s",
                            assigned_uid, goal_id,
                        )
        except Exception as exc:
            _log.warning("Notion story.state_changed: member page notify failed story=%s: %s", story_id, exc)
