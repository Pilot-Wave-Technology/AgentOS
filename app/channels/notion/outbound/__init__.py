"""Notion outbound relay — post agent responses as Notion comments.

Entry point: relay_agent_message(goal_id, thread_id, content, msg_id).
Called from chat.py / goal_thread.py after every agent chat:final event.
No-op if there is no active Notion binding for the thread.
"""
from .agent import relay_agent_message, relay_human_message, relay_file_upload
from .system import post_to_goal_page, post_to_thread_page, relay_orchestrator_message

__all__ = [
    "relay_agent_message",
    "relay_human_message",
    "relay_file_upload",
    "post_to_goal_page",
    "post_to_thread_page",
    "relay_orchestrator_message",
]
