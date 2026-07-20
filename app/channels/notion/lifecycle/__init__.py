"""Notion lifecycle event relay.

Receives standard platform-agnostic lifecycle events from base.relay_lifecycle_event
and updates Notion databases / cards accordingly.

Registered in channels/registry.py as _LIFECYCLE_RELAYS["notion"] so callers in
stories.py, orchestrator.py, etc. only need to call relay_lifecycle_event() once —
this module handles the Notion side.

Architecture: Notion cards (stories, tasks) store AgentOS entity IDs as properties.
When we need to update a card we query Notion by our ID rather than storing Notion's
page ID in our tables. This keeps AgentOS tables channel-agnostic.
"""
import logging

from .story import _on_story_created, _on_story_state_changed
from .task import _on_task_created, _on_task_state_changed
from .goal import (
    _on_orchestrator_message, _on_member_joined, _on_member_left, _on_resource_registered,
    _on_goal_completed, _on_goal_deleted,
)

_log = logging.getLogger(__name__)


async def relay_lifecycle_event(
    goal_id: str,
    event_type: str,
    payload: dict,
    thread_id: str | None = None,
) -> None:
    """Dispatch a standard lifecycle event to the relevant Notion pages/databases.

    Supported event types:
      story.state_changed  → story_id, story_title, old_state, new_state
      story.created        → story_id, story_title, assigned_to (optional)
      task.created         → task_id, task_title, story_id
      task.state_changed   → task_id, task_title, story_id, old_state, new_state
      task.completed       → task_id, task_title, story_id, result_summary (optional)
      member.joined        → uid, display_name
      member.left          → uid, display_name
      resource.registered  → resource_id, filename, file_type, file_size_kb, uploader_uid
      goal.completed       → goal_name
      goal.deleted         → goal_name
      orchestrator.message → content, username
    """
    try:
        if event_type == "story.state_changed":
            await _on_story_state_changed(goal_id, payload, thread_id)
        elif event_type == "story.created":
            await _on_story_created(goal_id, payload)
        elif event_type == "task.created":
            await _on_task_created(goal_id, payload)
        elif event_type in ("task.state_changed", "task.completed"):
            await _on_task_state_changed(goal_id, payload)
        elif event_type == "member.joined":
            await _on_member_joined(goal_id, payload)
        elif event_type == "member.left":
            await _on_member_left(goal_id, payload)
        elif event_type == "resource.registered":
            await _on_resource_registered(goal_id, payload)
        elif event_type == "goal.completed":
            await _on_goal_completed(goal_id, payload)
        elif event_type == "goal.deleted":
            await _on_goal_deleted(goal_id, payload)
        elif event_type == "orchestrator.message":
            await _on_orchestrator_message(goal_id, payload)
        else:
            _log.debug("Notion lifecycle: unhandled event_type=%s goal=%s", event_type, goal_id)
    except Exception as exc:
        _log.warning("Notion lifecycle relay error: event=%s goal=%s: %s", event_type, goal_id, exc)
