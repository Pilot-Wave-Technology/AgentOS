"""State transition maps and validators for AgentOS entity types.

Each sub-module owns one entity. Import from this package, not sub-modules directly.
"""
from .tools import assert_tool_transition, TOOL_ACTIVE_STATUSES, TOOL_TRANSITIONS

__all__ = ["assert_tool_transition", "TOOL_ACTIVE_STATUSES", "TOOL_TRANSITIONS"]
