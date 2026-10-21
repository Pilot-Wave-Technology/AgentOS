"""The Goal Orchestrator's kernel surface.

Deliberately a separate package from `kernel.assistant`. The assistant
asks and GO decides, and that split is only real if the two have
different surfaces -- an assistant that could reach `grant_resource` by
naming it would make the arrangement a convention rather than a
boundary.
"""

from kernel.go.brief import resource_brief
from kernel.go.goal_brief import read_brief, retire_decision, update_brief
from kernel.go.code_state import code_state
from kernel.go.accept_errand import accept_errand
from kernel.go.errands import propose_errand
from kernel.go.preview import plan_preview
from kernel.go.state import goal_state
from kernel.go.read import read_resource  # noqa: F401
from kernel.go.members import goal_members
from kernel.go.tools import (TOOLS, answer_request, grant_resource,
                             list_requests, register_resource,
                             update_goal)

__all__ = ["TOOLS", "accept_errand", "answer_request", "code_state",
           "goal_state",
           "goal_members",
           "propose_errand",
           "resource_brief", "read_brief", "retire_decision", "update_brief",
           "grant_resource", "list_requests", "plan_preview",
           "register_resource", "update_goal"]
