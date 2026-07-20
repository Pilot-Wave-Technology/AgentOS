"""Query helpers — find Notion cards by AgentOS entity IDs.

SDK compatibility: notion-client 2.x uses databases.query(); 3.x uses
data_sources.query(data_source_id, ...). We try data_sources first and fall back
to databases.query so either SDK version works.
"""
import logging

_log = logging.getLogger(__name__)


async def _get_ds_id(db_id: str, notion) -> str:
    """Return the primary data_source_id for a database.

    Falls back to db_id if retrieve fails or data_sources is absent.
    """
    try:
        db = await notion.databases.retrieve(db_id)
        sources = db.get("data_sources") or []
        return sources[0]["id"] if sources else db_id
    except Exception:
        return db_id


async def _query_db(notion, db_id: str, **kwargs) -> dict:
    """Query a database using data_sources.query (SDK 3.x) or databases.query (SDK 2.x)."""
    ds_id = await _get_ds_id(db_id, notion)
    if hasattr(notion, "data_sources"):
        return await notion.data_sources.query(ds_id, **kwargs)
    return await notion.databases.query(db_id, **kwargs)


async def find_story_card(stories_board_db_id: str, story_id: str, notion) -> str | None:
    """Query the Stories Board for a card whose 'Story ID' property matches story_id.

    Returns the Notion page ID or None if not found.
    Notion stores our AgentOS ID; we look up by it rather than storing Notion's ID.
    """
    try:
        results = await _query_db(
            notion, stories_board_db_id,
            filter={"property": "Story ID", "rich_text": {"equals": str(story_id)}},
        )
        pages = results.get("results", [])
        if pages:
            return pages[0]["id"]
        _log.debug("find_story_card: no card found db=%s story=%s", stories_board_db_id, story_id)
    except Exception as exc:
        _log.warning("find_story_card failed db=%s story=%s: %s", stories_board_db_id, story_id, exc)
    return None


async def find_task_card(tasks_db_id: str, task_id: str, notion) -> str | None:
    """Query a Tasks Feed database for a card whose 'Task ID' property matches task_id."""
    try:
        results = await _query_db(
            notion, tasks_db_id,
            filter={"property": "Task ID", "rich_text": {"equals": str(task_id)}},
        )
        pages = results.get("results", [])
        if pages:
            return pages[0]["id"]
    except Exception as exc:
        _log.warning("find_task_card failed db=%s task=%s: %s", tasks_db_id, task_id, exc)
    return None


async def find_story_tasks_db(story_page_id: str, notion) -> str | None:
    """Find the Tasks Feed sub-database on a story card page by scanning child databases."""
    try:
        blocks = await notion.blocks.children.list(block_id=story_page_id)
        for block in blocks.get("results", []):
            if block.get("type") == "child_database":
                db_title = block.get("child_database", {}).get("title", "")
                if "Tasks" in db_title or "tasks" in db_title:
                    return block["id"]
    except Exception as exc:
        _log.warning("find_story_tasks_db failed page=%s: %s", story_page_id, exc)
    return None


async def fetch_existing_story_ids(db_id: str, notion) -> set[str]:
    """Return the set of AgentOS story IDs already present as cards in a Stories Board database.

    Used during re-provisioning to skip stories that already have cards.
    """
    existing: set[str] = set()
    try:
        cursor = None
        while True:
            kwargs: dict = {}
            if cursor:
                kwargs["start_cursor"] = cursor
            results = await _query_db(notion, db_id, **kwargs)
            for page in results.get("results", []):
                rt = page.get("properties", {}).get("Story ID", {}).get("rich_text", [])
                sid = rt[0].get("plain_text", "") if rt else ""
                if sid:
                    existing.add(sid)
            if not results.get("has_more"):
                break
            cursor = results.get("next_cursor")
    except Exception as exc:
        _log.warning("fetch_existing_story_ids failed db=%s: %s", db_id, exc)
    return existing
