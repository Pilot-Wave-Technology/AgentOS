"""Block primitives, Notion property constants, and state mappings shared across pages sub-modules."""
import logging

_log = logging.getLogger(__name__)

_WARNING_HEADER = [
    {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": "⚠️ Managed by AgentOS. Do not edit directly — changes will be overwritten."}}],
            "icon": {"type": "emoji", "emoji": "⚠️"},
            "color": "yellow_background",
        },
    }
]

_STORY_STATUS_OPTIONS = [
    {"name": "Proposed",    "color": "gray"},
    {"name": "Accepted",    "color": "blue"},
    {"name": "In Progress", "color": "yellow"},
    {"name": "Blocked",     "color": "red"},
    {"name": "Done",        "color": "green"},
    {"name": "Rejected",    "color": "orange"},
    {"name": "Cancelled",   "color": "pink"},
]

_TASK_STATUS_OPTIONS = [
    {"name": "pending",           "color": "gray"},
    {"name": "ready",             "color": "yellow"},
    {"name": "dispatching",       "color": "blue"},
    {"name": "in_progress",       "color": "green"},
    {"name": "blocked",           "color": "red"},
    {"name": "automated_review",  "color": "purple"},
    {"name": "awaiting_approval", "color": "orange"},
    {"name": "approved",          "color": "blue"},
    {"name": "done",              "color": "green"},
    {"name": "failed",            "color": "red"},
    {"name": "cancelled",         "color": "gray"},
]

# Maps AgentOS story states to Notion Status label strings
STORY_STATE_TO_NOTION = {
    "proposed":    "Proposed",
    "accepted":    "Accepted",
    "in_progress": "In Progress",
    "blocked":     "Blocked",
    "done":        "Done",
    "rejected":    "Rejected",
    "cancelled":   "Cancelled",
}


def _text(content: str) -> dict:
    return {"type": "text", "text": {"content": content}}


def _heading(level: int, content: str) -> dict:
    return {
        "object": "block",
        "type": f"heading_{level}",
        f"heading_{level}": {"rich_text": [_text(content)]},
    }


def _paragraph(content: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_text(content)]},
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}
