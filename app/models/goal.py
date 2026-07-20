from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel


GoalState = Literal["planning", "active", "paused", "completed", "cancelled"]

VALID_GOAL_TRANSITIONS: dict[str, list[str]] = {
    "planning": ["active", "cancelled"],
    "active": ["paused", "completed", "cancelled"],
    "paused": ["active", "cancelled"],
    "completed": [],
    "cancelled": [],
}


class GoalCreate(BaseModel):
    name: str
    description: str = ""
    objective: str = ""
    deadline: Optional[datetime] = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    color_tag: str = "#6366f1"   # indigo default


class GoalSettings(BaseModel):
    batch_window_seconds: float = 10.0
    standup_cron: str = ""                  # e.g., "0 9 * * 1-5"
    auto_unblock: bool = True
    require_approval_for_done: bool = False
    max_concurrent_tasks: int = 10
    token_budget: Optional[int] = None
    notification_level: Literal["quiet", "normal", "verbose"] = "normal"


class Goal(BaseModel):
    id: str
    name: str
    description: str = ""
    objective: str = ""
    deadline: Optional[datetime] = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    color_tag: str = "#6366f1"
    state: GoalState = "active"
    completion_pct: int = 0
    go_agent_id: str = ""
    go_thread_id: str = ""
    created_by: str
    created_at: Optional[datetime] = None
    status: Literal["active", "deleted"] = "active"




class MemberRecord(BaseModel):
    uid: str
    role: Literal["admin", "member"] = "member"  # GoalRole.GOAL_ADMIN / GoalRole.GOAL_MEMBER
    added_at: Optional[datetime] = None
    added_by: str = ""


class JoinRequest(BaseModel):
    uid: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    requested_at: Optional[datetime] = None
    message: str = ""
