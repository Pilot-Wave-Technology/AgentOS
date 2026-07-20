"""Story card, task card, orchestrator event, and property update operations."""
import logging

from ._blocks import _text, STORY_STATE_TO_NOTION

_log = logging.getLogger(__name__)


async def create_story_card(
    stories_board_db_id: str,
    story_id: str,
    title: str,
    state: str,
    assigned_to: str,
    priority: str,
    notion,
    access_token: str = "",
) -> tuple[str, str, str]:
    """Create a story card in the Stories Board and a Tasks Feed inline DB on its page.

    Returns (story_page_id, tasks_db_id, tasks_ds_id).
    Story ID stores the AgentOS UUID so Notion can be queried by our ID.
    """
    import datetime
    from .databases import create_tasks_feed_database

    notion_status = STORY_STATE_TO_NOTION.get(state, "Proposed")
    properties: dict = {
        "Name":     {"title": [_text(title)]},
        "Status":   {"select": {"name": notion_status}},
        "Story ID": {"rich_text": [_text(str(story_id))]},
        "Created":  {"date": {"start": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")}},
    }
    if assigned_to:
        properties["Assigned To"] = {"rich_text": [_text(assigned_to)]}
    if priority:
        properties["Priority"] = {"select": {"name": priority}}

    page = await notion.pages.create(
        parent={"type": "database_id", "database_id": stories_board_db_id},
        properties=properties,
        children=[{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": "💬 To chat with your assistant, use your personal page in the Members section of this goal. This story page is view-only.",
                }}],
                "icon": {"type": "emoji", "emoji": "💬"},
                "color": "blue_background",
            },
        }],
    )
    story_page_id = page["id"]
    _log.info("Created story card page=%s story=%s", story_page_id, story_id)

    tasks_db_id, tasks_ds_id = await create_tasks_feed_database(
        story_page_id, title, notion, access_token,
    )
    return story_page_id, tasks_db_id, tasks_ds_id


async def create_task_card(
    tasks_db_id: str,
    task_id: str,
    title: str,
    state: str,
    notion,
) -> str:
    """Create a task card in a Tasks Feed database. Returns the Notion page ID."""
    import datetime
    page = await notion.pages.create(
        parent={"type": "database_id", "database_id": tasks_db_id},
        properties={
            "Name":    {"title": [_text(title)]},
            "Status":  {"select": {"name": state}},
            "Task ID": {"rich_text": [_text(str(task_id))]},
            "Created": {"date": {"start": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")}},
        },
    )
    page_id = page["id"]
    _log.info("Created task card page=%s task=%s", page_id, task_id)
    return page_id


async def update_page_select_property(page_id: str, property_name: str, value: str, notion) -> None:
    """Set a select property on a Notion page to the given value."""
    try:
        await notion.pages.update(
            page_id,
            properties={property_name: {"select": {"name": value}}},
        )
        _log.debug("Updated %s.%s = %s", page_id, property_name, value)
    except Exception as exc:
        _log.warning("update_page_select_property failed page=%s prop=%s val=%s: %s", page_id, property_name, value, exc)


async def append_orchestrator_event(
    orch_db_id: str,
    event_title: str,
    event_type: str,
    actor: str,
    notion,
    timestamp: str | None = None,
) -> None:
    """Append a new row to the Orchestrator timeline database.

    actor must be one of: "System", "Agent", "User".
    event_type should match one of the Type select options on the DB.
    """
    import datetime
    ts = timestamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        await notion.pages.create(
            parent={"type": "database_id", "database_id": orch_db_id},
            properties={
                "Event":     {"title": [_text(event_title[:2000])]},
                "Timestamp": {"date": {"start": ts}},
                "Actor":     {"select": {"name": actor}},
                "Type":      {"select": {"name": event_type}},
            },
        )
        _log.debug("append_orchestrator_event db=%s type=%s", orch_db_id, event_type)
    except Exception as exc:
        _log.warning("append_orchestrator_event failed db=%s type=%s: %s", orch_db_id, event_type, exc)


async def append_feed_row(
    db_id: str,
    title: str,
    event_type: str,
    actor: str,
    details: str,
    notion,
    timestamp: str | None = None,
) -> None:
    """Append a new row to a Resources Feed database.

    Kept for the Resources Feed (lifecycle/goal.py resource.registered handler)
    which uses a different schema than the Orchestrator DB.
    """
    import datetime
    ts = timestamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        await notion.pages.create(
            parent={"type": "database_id", "database_id": db_id},
            properties={
                "Event":      {"title": [_text(title[:2000])]},
                "Event Type": {"select": {"name": event_type}},
                "Timestamp":  {"date": {"start": ts}},
                "Actor":      {"rich_text": [_text(actor[:2000])] if actor else []},
                "Details":    {"rich_text": [_text(details[:2000])] if details else []},
            },
        )
        _log.debug("append_feed_row db=%s event=%s", db_id, event_type)
    except Exception as exc:
        _log.warning("append_feed_row failed db=%s event=%s: %s", db_id, event_type, exc)
