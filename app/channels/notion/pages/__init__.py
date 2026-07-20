"""Notion page and database provisioning — creates goal structure in a Notion workspace.

Goal structure:
    [Goal Page]  (database row in parent Goals database)
    ├── Stories Board  (inline database, Board view grouped by Status)
    │    Story pages have a Tasks Feed inline database and serve as the SHCA
    │    conversation surface — comments route to the assigned user's SHCA thread.
    └── Orchestrator  (inline database, Timeline view — read-only activity log)

Each Story card page:
    └── Tasks Feed  (inline database, Board view grouped by Status)

Entity reference design: Notion cards store AgentOS IDs (Story ID, Task ID) as
properties. AgentOS tables never store Notion page IDs — this keeps core tables
channel-agnostic when multiple platforms are enabled simultaneously.
"""
from ._blocks import STORY_STATE_TO_NOTION
from .databases import (
    create_goal_structure,
    create_stories_board_database,
    create_orchestrator_db,
    create_orchestrator_feed_database,
    create_resources_feed_database,
    create_tasks_feed_database,
    setup_gallery_view_on_db,
)
from .cards import (
    create_story_card,
    create_task_card,
    update_page_select_property,
    append_orchestrator_event,
    append_feed_row,
)
from .lookup import (
    find_story_card,
    find_task_card,
    find_story_tasks_db,
    fetch_existing_story_ids,
)
from .members import (
    create_member_subpage,
    update_orchestrator_page,
)

__all__ = [
    "STORY_STATE_TO_NOTION",
    "create_goal_structure",
    "create_stories_board_database",
    "create_orchestrator_db",
    "create_orchestrator_feed_database",
    "create_resources_feed_database",
    "create_tasks_feed_database",
    "setup_gallery_view_on_db",
    "create_story_card",
    "create_task_card",
    "update_page_select_property",
    "append_orchestrator_event",
    "append_feed_row",
    "find_story_card",
    "find_task_card",
    "find_story_tasks_db",
    "fetch_existing_story_ids",
    "create_member_subpage",
    "update_orchestrator_page",
]
