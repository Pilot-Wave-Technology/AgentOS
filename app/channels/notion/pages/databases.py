"""Notion database creation — goal structure, stories board, feed databases.

SDK note: notion-client >= 3.x uses Notion-Version 2026-03-11 where
databases.create requires properties under `initial_data_source.properties`
(not top-level `properties`), and databases.query is replaced by
data_sources.query. All creation functions return (db_id, data_source_id)
tuples so callers can query and create rows correctly.

View creation (/v1/views) is not exposed by the SDK — we call it directly via
httpx. Board views require the internal property ID of the group-by column,
which databases.retrieve does NOT return on this API version. We work around
this by creating a temporary page, calling pages.retrieve to get the ID, then
archiving the page before creating the view.
"""
import logging

import httpx

from ._blocks import (
    _text, _heading, _paragraph, _divider, _WARNING_HEADER,
    _STORY_STATUS_OPTIONS, _TASK_STATUS_OPTIONS,
)

_NOTION_VERSION  = "2026-03-11"
_NOTION_VIEWS_URL = "https://api.notion.com/v1/views"
_HTTP_TIMEOUT    = 15

_log = logging.getLogger(__name__)


def _ds_id(db: dict) -> str:
    """Extract the primary data source ID from a databases.create response."""
    sources = db.get("data_sources") or []
    return sources[0]["id"] if sources else db["id"]


async def _notion_api(access_token: str, method: str, url: str, **kwargs) -> httpx.Response:
    """Shared httpx helper for direct Notion REST calls not covered by the SDK."""
    headers = {
        "Authorization":  f"Bearer {access_token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type":   "application/json",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
        return await getattr(http, method)(url, headers=headers, **kwargs)


async def _get_prop_id(notion, page_id: str, prop_name: str) -> str:
    """Return Notion's internal property ID for a named property via pages.retrieve.

    databases.retrieve returns empty IDs on this API version; page retrieve always works.
    """
    try:
        page = await notion.pages.retrieve(page_id=page_id)
        return (page.get("properties") or {}).get(prop_name, {}).get("id", "")
    except Exception as exc:
        _log.debug("_get_prop_id failed page=%s prop=%s: %s", page_id, prop_name, exc)
        return ""


async def _delete_auto_table_views(access_token: str, db_id: str) -> None:
    """Delete auto-created table views from a database using the direct REST API.

    The SDK's create response does not include view IDs on this API version.
    A fresh GET with the newer version does expose them.
    """
    try:
        resp = await _notion_api(access_token, "get", f"https://api.notion.com/v1/databases/{db_id}")
        if resp.status_code == 200:
            for source in (resp.json().get("data_sources") or []):
                for view in (source.get("views") or []):
                    if view.get("type") == "table":
                        try:
                            await _notion_api(access_token, "delete", f"{_NOTION_VIEWS_URL}/{view['id']}")
                            _log.info("Deleted table view %s from db=%s", view["id"], db_id)
                        except Exception as exc:
                            _log.debug("Could not delete view %s: %s", view["id"], exc)
        else:
            _log.debug("Could not fetch DB for table-view cleanup db=%s: HTTP %s", db_id, resp.status_code)
    except Exception as exc:
        _log.warning("_delete_auto_table_views db=%s: %s", db_id, exc)


async def _create_view(
    access_token: str,
    db_id: str,
    ds_id: str,
    name: str,
    view_type: str,
    configuration: dict,
    position_type: str = "start",
) -> None:
    """POST /v1/views — create a view on a database.

    Requires data_source_id and a configuration block with type set.
    Failures are logged as warnings and do not raise — view creation is best-effort.
    """
    if not access_token or not db_id or not ds_id:
        return
    try:
        resp = await _notion_api(access_token, "post", _NOTION_VIEWS_URL, json={
            "database_id":    db_id,
            "data_source_id": ds_id,
            "name":           name,
            "type":           view_type,
            "position":       {"type": position_type},
            "configuration":  configuration,
        })
        if resp.status_code in (200, 201):
            _log.info("Created %s view db=%s view=%s", view_type, db_id, resp.json().get("id", ""))
        else:
            _log.warning("View creation failed type=%s db=%s: HTTP %s %s", view_type, db_id, resp.status_code, resp.text[:300])
    except Exception as exc:
        _log.warning("View creation error type=%s db=%s: %s", view_type, db_id, exc)


async def _setup_board_view(
    access_token: str,
    notion,
    db_id: str,
    ds_id: str,
    view_name: str,
    title_prop_name: str = "Name",
    status_prop_name: str = "Status",
    first_status_option: str = "",
) -> None:
    """Create a board view grouped by a select property.

    Gets the internal property ID from a temporary page (databases.retrieve does
    not expose property IDs on this API version). Archives the temp page before
    creating the view, then removes the auto-created table view.
    """
    state_prop_id = ""
    temp_page_id  = None
    try:
        props: dict = {title_prop_name: {"title": [_text("__temp__")]}}
        if first_status_option:
            props[status_prop_name] = {"select": {"name": first_status_option}}
        temp_page = await notion.pages.create(
            parent={"type": "database_id", "database_id": db_id},
            properties=props,
        )
        temp_page_id  = temp_page["id"]
        state_prop_id = await _get_prop_id(notion, temp_page_id, status_prop_name)
        _log.debug("Board view prop id db=%s prop=%s id=%s", db_id, status_prop_name, state_prop_id)
    except Exception as exc:
        _log.warning("Could not seed temp page for board view db=%s: %s", db_id, exc)

    if temp_page_id:
        try:
            await notion.pages.update(temp_page_id, archived=True)
        except Exception as _exc:
            _log.debug("Could not archive temp board-view seed page %s: %s", temp_page_id, _exc)

    configuration: dict = {"type": "board"}
    if state_prop_id:
        configuration["group_by"] = {
            "type":        "select",
            "property_id": state_prop_id,
            "sort":        {"type": "manual"},
        }
    else:
        _log.warning("No %s prop ID for board view db=%s — board will not be grouped", status_prop_name, db_id)

    await _create_view(access_token, db_id, ds_id, view_name, "board", configuration)
    await _delete_auto_table_views(access_token, db_id)


async def setup_gallery_view_on_db(
    access_token: str,
    db_id: str,
    ds_id: str,
    view_name: str = "Goals",
) -> None:
    """Add a gallery view to an existing database. Best-effort; does not raise."""
    await _create_view(access_token, db_id, ds_id, view_name, "gallery",
                       {"type": "gallery"})
    await _delete_auto_table_views(access_token, db_id)


async def create_goal_structure(
    goal_id: str,
    goal_name: str,
    goal_objective: str,
    goal_status: str,
    members: list[dict],
    parent_database_id: str,
    access_token: str,
) -> dict:
    """Create the full Notion structure for a goal.

    Layout (top to bottom on the goal page):
      1. Inline Stories Board (board view) — conversations happen in story page comments
      2. Divider + Objective / Team / instructions
      3. Inline Orchestrator DB (timeline view) — read-only activity log

    Returns:
        {
            "goal_page_id": str,
            "stories_board_db_id": str,
            "stories_board_ds_id": str,
            "orch_db_id": str,
            "orch_ds_id": str,
        }
    """
    from notion_client import AsyncClient
    notion = AsyncClient(auth=access_token)

    # Create the goal page first (no children — inline databases must be appended
    # after page creation via databases.create with is_inline=True).
    goal_page = await notion.pages.create(
        parent={"type": "database_id", "database_id": parent_database_id},
        properties={
            "title": {"title": [_text(goal_name)]},
        },
    )
    goal_page_id = goal_page["id"]
    _log.info("Created goal page for goal=%s page=%s", goal_id, goal_page_id)

    # Stories Board — inline at top, conversation surface per story.
    stories_board_db_id, stories_board_ds_id = await create_stories_board_database(
        goal_page_id, goal_name, notion, access_token,
    )

    # Text content between Stories Board and Orchestrator.
    member_lines = [
        _paragraph(f"• {m.get('display_name') or m.get('email') or m.get('uid', '')}")
        for m in members
    ]
    try:
        await notion.blocks.children.append(
            block_id=goal_page_id,
            children=[
                _divider(),
                _heading(2, "Objective"),
                _paragraph(goal_objective or "No objective set."),
                _divider(),
                _heading(2, "Team"),
                *member_lines,
                _divider(),
                _paragraph(
                    "Comment on a story page to chat with the story's assistant. "
                    "Only the story's assigned person can use the chat."
                ),
            ],
        )
    except Exception as exc:
        _log.warning("Could not append text blocks to goal page=%s: %s", goal_page_id, exc)

    # Orchestrator DB — DISABLED for goals provisioned from now on. Agent
    # Mesh / Goal Review Council / Security Council are staying in-app-only
    # for new goals (see council_thread.py, security_thread.py,
    # orchestrator.py); goals that already have an orch_db_id keep working
    # unchanged (relay_orchestrator_message no-ops when it's missing).
    # Commented out, not deleted — intended to come back later.
    # orch_db_id, orch_ds_id = await create_orchestrator_db(
    #     goal_page_id, goal_name, notion, access_token,
    # )
    orch_db_id, orch_ds_id = "", ""

    return {
        "goal_page_id":        goal_page_id,
        "stories_board_db_id": stories_board_db_id,
        "stories_board_ds_id": stories_board_ds_id,
        "orch_db_id":          orch_db_id,
        "orch_ds_id":          orch_ds_id,
    }


async def create_stories_board_database(
    goal_page_id: str,
    goal_name: str,
    notion,
    access_token: str = "",
) -> tuple[str, str]:
    """Create the Stories Board as an inline database on the goal page.

    Full AgentOS story schema. Story ID stores the AgentOS UUID used for lookups.
    Board view grouped by Status is created after seeding a temp page for property IDs.
    Returns (database_id, data_source_id).
    """
    db = await notion.databases.create(
        parent={"type": "page_id", "page_id": goal_page_id},
        title=[_text(f"{goal_name} — Stories")],
        is_inline=True,
        initial_data_source={
            "properties": {
                "Name":                {"title": {}},
                "Status":              {"select": {"options": _STORY_STATUS_OPTIONS}},
                "Assigned To":         {"rich_text": {}},
                "Priority":            {"select": {"options": [
                    {"name": "low", "color": "gray"},
                    {"name": "medium", "color": "yellow"},
                    {"name": "high", "color": "orange"},
                    {"name": "critical", "color": "red"},
                ]}},
                "Story ID":            {"rich_text": {}},
                "Estimated Effort":    {"rich_text": {}},
                "Description":         {"rich_text": {}},
                "Acceptance Criteria": {"rich_text": {}},
                "Blocked Reason":      {"rich_text": {}},
                "Result Summary":      {"rich_text": {}},
                "Started At":          {"date": {}},
                "Completed At":        {"date": {}},
                "Created":             {"date": {}},
            },
        },
    )
    db_id = db["id"]
    ds_id = _ds_id(db)
    _log.info("Created Stories Board db=%s ds=%s for goal_page=%s", db_id, ds_id, goal_page_id)

    if access_token:
        first_status = _STORY_STATUS_OPTIONS[0]["name"] if _STORY_STATUS_OPTIONS else ""
        await _setup_board_view(
            access_token, notion, db_id, ds_id,
            view_name="Board",
            first_status_option=first_status,
        )

    try:
        await notion.databases.update(
            database_id=db_id,
            is_locked=True,
            is_inline=True,
        )
        _log.info("Locked Stories Board db=%s", db_id)
    except Exception as exc:
        _log.debug("Could not lock Stories Board db=%s: %s", db_id, exc)

    return db_id, ds_id


async def create_orchestrator_db(
    goal_page_id: str,
    goal_name: str,
    notion,
    access_token: str = "",
) -> tuple[str, str]:
    """Create the Orchestrator as an inline timeline database on the goal page.

    Each row is a timestamped system/agent event — the read-only activity log for a goal.
    Seeds a 'goal activated' event immediately so the Timestamp property ID is available
    for timeline view creation. Returns (database_id, data_source_id).
    """
    import datetime

    db = await notion.databases.create(
        parent={"type": "page_id", "page_id": goal_page_id},
        title=[_text("Orchestrator")],
        is_inline=True,
        initial_data_source={
            "properties": {
                "Event":     {"title": {}},
                "Timestamp": {"date": {}},
                "Actor":     {"select": {"options": [
                    {"name": "System", "color": "gray"},
                    {"name": "Agent",  "color": "blue"},
                    {"name": "User",   "color": "green"},
                ]}},
                "Type":      {"select": {"options": [
                    {"name": "goal.created"},
                    {"name": "story.created"},
                    {"name": "story.state_changed"},
                    {"name": "task.created"},
                    {"name": "task.state_changed"},
                    {"name": "task.completed"},
                    {"name": "member.joined"},
                    {"name": "member.left"},
                    {"name": "orchestrator.message"},
                    {"name": "goal.completed"},
                    {"name": "resource.registered"},
                ]}},
            },
        },
    )
    db_id = db["id"]
    ds_id = _ds_id(db)
    _log.info("Created Orchestrator DB db=%s ds=%s for goal_page=%s", db_id, ds_id, goal_page_id)

    # Lock the DB immediately (read-only by convention).
    try:
        await notion.databases.update(
            database_id=db_id,
            is_locked=True,
            is_inline=True,
            icon={"type": "emoji", "emoji": "🗓️"},
        )
    except Exception as exc:
        _log.debug("Could not lock Orchestrator DB db=%s: %s", db_id, exc)

    if not access_token:
        return db_id, ds_id

    # Seed the first event so we can retrieve the Timestamp property ID for the timeline view.
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    sample_page_id = None
    try:
        page = await notion.pages.create(
            parent={"type": "database_id", "database_id": db_id},
            properties={
                "Event":     {"title": [_text(f"Goal \"{goal_name}\" activated")]},
                "Timestamp": {"date": {"start": ts}},
                "Actor":     {"select": {"name": "System"}},
                "Type":      {"select": {"name": "goal.created"}},
            },
        )
        sample_page_id = page["id"]
        _log.debug("Seeded goal.created event page=%s db=%s", sample_page_id, db_id)
    except Exception as exc:
        _log.warning("Could not seed orchestrator event db=%s: %s", db_id, exc)

    if sample_page_id:
        timestamp_id = await _get_prop_id(notion, sample_page_id, "Timestamp")
        if timestamp_id:
            try:
                resp = await _notion_api(access_token, "post", _NOTION_VIEWS_URL, json={
                    "database_id":    db_id,
                    "data_source_id": ds_id,
                    "name":           "Timeline",
                    "type":           "timeline",
                    "timeline_by":    "hour",
                    "position":       {"type": "start"},
                    "configuration":  {
                        "type":             "timeline",
                        "date_property_id": timestamp_id,
                        "show_table":       True,
                        "timeline_by":      "hour",
                    },
                })
                if resp.status_code in (200, 201):
                    _log.info("Created timeline view db=%s view=%s", db_id, resp.json().get("id", ""))
                else:
                    _log.warning("Timeline view HTTP %s db=%s: %s", resp.status_code, db_id, resp.text[:300])
            except Exception as exc:
                _log.warning("Timeline view creation failed db=%s: %s", db_id, exc)
        await _delete_auto_table_views(access_token, db_id)

    return db_id, ds_id


async def create_orchestrator_feed_database(
    goal_page_id: str,
    goal_name: str,
    notion,
    access_token: str = "",
) -> tuple[str, str]:
    """Deprecated: delegates to create_orchestrator_db.

    Kept so any callers that still reference this name continue to work.
    """
    _log.info("create_orchestrator_feed_database: delegating to create_orchestrator_db goal_page=%s", goal_page_id)
    return await create_orchestrator_db(goal_page_id, goal_name, notion, access_token)


async def create_resources_feed_database(
    goal_page_id: str,
    goal_name: str,
    notion,
) -> tuple[str, str]:
    """Create the Resources Feed as a sub-page database (read-only by convention).

    Returns (database_id, data_source_id).
    """
    db = await notion.databases.create(
        parent={"type": "page_id", "page_id": goal_page_id},
        title=[_text(f"{goal_name} — Resources")],
        initial_data_source={
            "properties": {
                "Name":        {"title": {}},
                "File Type":   {"select": {}},
                "Size KB":     {"number": {}},
                "Uploaded By": {"rich_text": {}},
                "Resource ID": {"rich_text": {}},
                "Added":       {"date": {}},
            },
        },
    )
    db_id = db["id"]
    ds_id = _ds_id(db)
    _log.info("Created Resources Feed db=%s ds=%s for goal_page=%s", db_id, ds_id, goal_page_id)
    return db_id, ds_id


async def create_tasks_feed_database(
    story_page_id: str,
    story_title: str,
    notion,
    access_token: str = "",
) -> tuple[str, str]:
    """Create the Tasks Feed inline database on a story card page.

    Full AgentOS task schema. Task ID stores the AgentOS UUID used for lookups.
    Board view grouped by Status is created after seeding a temp page for property IDs.
    Appends a warning header to the story page. Returns (database_id, data_source_id).
    """
    db = await notion.databases.create(
        parent={"type": "page_id", "page_id": story_page_id},
        title=[_text(f"{story_title} — Tasks")],
        is_inline=True,
        initial_data_source={
            "properties": {
                "Name":        {"title": {}},
                "Status":      {"select": {"options": _TASK_STATUS_OPTIONS}},
                "Task ID":     {"rich_text": {}},
                "Priority":    {"select": {"options": [
                    {"name": "low",      "color": "gray"},
                    {"name": "medium",   "color": "yellow"},
                    {"name": "high",     "color": "orange"},
                    {"name": "critical", "color": "red"},
                ]}},
                "Assigned To": {"rich_text": {}},
                "Description": {"rich_text": {}},
                "Due At":      {"date": {}},
                "Result Summary": {"rich_text": {}},
                "Created":     {"date": {}},
            },
        },
    )
    db_id = db["id"]
    ds_id = _ds_id(db)

    try:
        await notion.blocks.children.append(block_id=story_page_id, children=_WARNING_HEADER)
    except Exception as exc:
        _log.debug("Could not append warning header for tasks feed: %s", exc)

    if access_token:
        first_status = _TASK_STATUS_OPTIONS[0]["name"] if _TASK_STATUS_OPTIONS else ""
        await _setup_board_view(
            access_token, notion, db_id, ds_id,
            view_name="Board",
            first_status_option=first_status,
        )

    try:
        await notion.databases.update(
            database_id=db_id,
            is_locked=True,
            is_inline=True,
        )
        _log.info("Locked Tasks Feed db=%s", db_id)
    except Exception as exc:
        _log.debug("Could not lock Tasks Feed db=%s: %s", db_id, exc)

    _log.info("Created Tasks Feed db=%s ds=%s for story_page=%s", db_id, ds_id, story_page_id)
    return db_id, ds_id
