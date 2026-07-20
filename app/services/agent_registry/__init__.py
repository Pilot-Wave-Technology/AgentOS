"""Agent registry service package.

Four sub-modules with single responsibilities:
  provision     — create an agent on the gateway and persist to DB
  update        — apply partial patches, sync to gateway, increment config_version
  decommission  — soft-delete and destroy gateway workspace (single agent)
  teardown      — bulk goal-level teardown (all agents owned by a deleted goal)

Import from this package, not from sub-modules directly.
"""
from .decommission import decommission_agent, soft_delete_agent
from .provision import provision_agent
from .teardown import teardown_goal_agents
from .update import update_agent

__all__ = [
    "provision_agent",
    "update_agent",
    "decommission_agent",
    "soft_delete_agent",
    "teardown_goal_agents",
]
