"""Member subpage creation and legacy orchestrator page helpers."""
import logging

from ._blocks import _text, _heading, _paragraph, _WARNING_HEADER

_log = logging.getLogger(__name__)


async def create_member_subpage(
    goal_id: str,
    goal_page_id: str,
    uid: str,
    display_name: str,
    access_token: str,
) -> str:
    """Create a per-member subpage under the goal page. Returns subpage ID."""
    from notion_client import AsyncClient
    notion = AsyncClient(auth=access_token)
    return await _create_member_subpage(notion, goal_id, goal_page_id, uid, display_name)


async def _create_member_subpage(
    notion,
    goal_id: str,
    goal_page_id: str,
    uid: str,
    display_name: str,
) -> str:
    try:
        subpage = await notion.pages.create(
            parent={"type": "page_id", "page_id": goal_page_id},
            properties={
                "title": {"title": [_text(f"{display_name}'s Assistant")]},
            },
            children=[
                _heading(1, f"{display_name}'s Assistant"),
                _paragraph("Use the comments section on this page to talk to your assistant."),
            ],
        )
        subpage_id = subpage["id"]
        _log.info("Created member subpage for goal=%s uid=%s page=%s", goal_id, uid, subpage_id)
        return subpage_id
    except Exception as exc:
        _log.error("Failed to create member subpage for goal=%s uid=%s: %s", goal_id, uid, exc)
        raise


async def update_orchestrator_page(
    page_id: str,
    content: str,
    access_token: str,
) -> None:
    """Overwrite orchestrator subpage content with latest agent update. Kept for backwards compat."""
    from notion_client import AsyncClient
    notion = AsyncClient(auth=access_token)

    existing = await notion.blocks.children.list(block_id=page_id)
    for block in existing.get("results", []):
        try:
            await notion.blocks.delete(block_id=block["id"])
        except Exception as _exc:
            _log.warning("Could not delete stale Notion block %s: %s", block["id"], _exc)

    await notion.blocks.children.append(
        block_id=page_id,
        children=_WARNING_HEADER + [
            _heading(1, "Orchestrator Log"),
            _paragraph(content),
        ],
    )
