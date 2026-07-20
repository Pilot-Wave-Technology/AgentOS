"""Task lifecycle event handlers — task.created and task.state_changed."""
import logging

from ._config import _get_notion_config

_log = logging.getLogger(__name__)


async def _on_task_created(goal_id: str, payload: dict) -> None:
    """Create a task card in the story's Tasks Feed and append an Orchestrator event."""
    nc = await _get_notion_config(goal_id)
    if not nc:
        return
    access_token, cfg, _integration_id = nc
    stories_board_db_id = cfg.get("stories_board_db_id", "")
    orch_db_id          = cfg.get("orch_db_id", "")
    if not stories_board_db_id:
        return

    task_id    = payload.get("task_id", "")
    task_title = payload.get("task_title", "")
    story_id   = payload.get("story_id", "")
    state      = payload.get("state", "pending")

    _log.info("Notion lifecycle: task.created goal=%s task=%s story=%s", goal_id, task_id, story_id)

    try:
        from notion_client import AsyncClient
        from app.channels.notion.pages import find_story_card, find_story_tasks_db, create_task_card
        notion = AsyncClient(auth=access_token)

        story_card_id = await find_story_card(stories_board_db_id, story_id, notion)
        if not story_card_id:
            _log.debug("Notion task.created: no story card found story=%s", story_id)
            return

        tasks_db_id = await find_story_tasks_db(story_card_id, notion)
        if not tasks_db_id:
            _log.debug("Notion task.created: no tasks db on story card story=%s", story_id)
            return

        await create_task_card(tasks_db_id, task_id, task_title, state, notion)
    except Exception as exc:
        _log.warning("Notion task.created: card creation failed task=%s: %s", task_id, exc)

    if orch_db_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import append_orchestrator_event
            notion = AsyncClient(auth=access_token)
            await append_orchestrator_event(
                orch_db_id,
                f"🔧 Task created: {task_title}",
                "task.created", "Agent", notion,
            )
        except Exception as exc:
            _log.warning("Notion task.created: orch event failed goal=%s: %s", goal_id, exc)


async def _on_task_state_changed(goal_id: str, payload: dict) -> None:
    """Update the task card Status in Notion and append an Orchestrator event.

    For awaiting_approval transitions, also posts a comment on the assigned
    story page so the assigned user is notified.
    """
    nc = await _get_notion_config(goal_id)
    if not nc:
        return
    access_token, cfg, _integration_id = nc
    stories_board_db_id = cfg.get("stories_board_db_id", "")
    orch_db_id          = cfg.get("orch_db_id", "")
    if not stories_board_db_id:
        return

    task_id    = payload.get("task_id", "")
    task_title = payload.get("task_title", "")
    story_id   = payload.get("story_id", "")
    new_state  = payload.get("new_state", "")

    _log.info("Notion lifecycle: task state changed goal=%s task=%s → %s", goal_id, task_id, new_state)

    story_page_id = None
    try:
        from notion_client import AsyncClient
        from app.channels.notion.pages import (
            find_story_card, find_story_tasks_db, find_task_card, update_page_select_property,
        )
        notion = AsyncClient(auth=access_token)

        story_card_id = await find_story_card(stories_board_db_id, story_id, notion) if story_id else None
        if story_card_id:
            story_page_id = story_card_id
            tasks_db_id   = await find_story_tasks_db(story_card_id, notion)
            if tasks_db_id and task_id:
                task_page_id = await find_task_card(tasks_db_id, task_id, notion)
                if task_page_id and new_state:
                    await update_page_select_property(task_page_id, "Status", new_state, notion)
    except Exception as exc:
        _log.warning("Notion task state changed: card update failed task=%s: %s", task_id, exc)

    if orch_db_id:
        try:
            from notion_client import AsyncClient
            from app.channels.notion.pages import append_orchestrator_event
            notion = AsyncClient(auth=access_token)
            old_state = payload.get("old_state", "")
            label = f"{task_title}: {old_state} → {new_state}" if old_state else f"{task_title}: {new_state}"
            await append_orchestrator_event(
                orch_db_id, label, "task.state_changed", "Agent", notion,
            )
        except Exception as exc:
            _log.warning("Notion task state changed: orch event failed goal=%s: %s", goal_id, exc)

    if new_state == "awaiting_approval":
        # Post approval request on the story page (the assigned user's conversation surface).
        msg = f"⏳ Task *{task_title}* is awaiting your approval. Please review and approve or reject."
        if story_page_id and access_token:
            try:
                from notion_client import AsyncClient
                from app.channels.notion.outbound._binding import _split_for_notion
                notion   = AsyncClient(auth=access_token)
                body     = f"[System] {msg}"
                chunks   = _split_for_notion(body)
                rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]
                await notion.comments.create(parent={"page_id": story_page_id}, rich_text=rich_text)
                _log.info(
                    "Notion task awaiting_approval: posted to story page=%s task=%s",
                    story_page_id, task_id,
                )
            except Exception as exc:
                _log.warning(
                    "Notion task.awaiting_approval: story page comment failed goal=%s task=%s: %s",
                    goal_id, task_id, exc,
                )
