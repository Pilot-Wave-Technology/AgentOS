from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel

TaskState = Literal[
    "pending",
    "ready",
    "dispatching",
    "in_progress",
    "blocked",
    "halted",          # kill switch: operator force-killed the agent. TERMINAL — no going back
    "automated_review",
    "awaiting_approval",
    "approved",
    "rejected",
    "done",
    "failed",
    "cancelled",
    # Story-specific states
    "proposed",
    "accepted",
]

VALID_TRANSITIONS: dict[str, list[str]] = {
    "pending":            ["ready", "in_progress", "cancelled", "failed"],
    "ready":              ["dispatching", "cancelled", "failed"],
    "dispatching":        ["in_progress", "halted", "failed"],
    "in_progress":        ["blocked", "halted", "automated_review", "awaiting_approval", "done", "failed"],
    # blocked→ready: allows a blocked subtask to be re-dispatched (dev git-engine/DAG feature)
    "blocked":            ["ready", "in_progress", "done", "cancelled", "failed"],
    # halted: operator force-killed the agent. TERMINAL — kill is a one-way door,
    # there is no restart. To redo the work, create a new task / start a new plan.
    "halted":             [],
    "automated_review":   ["awaiting_approval", "rejected", "failed"],
    "awaiting_approval":  ["approved", "rejected"],
    "approved":           ["done"],
    "rejected":           ["ready", "pending", "failed"],
    "done":               [],
    "failed":             ["ready", "pending"],  # allow retry
    "cancelled":          [],
    # Story-specific transitions
    "proposed":           ["accepted", "rejected"],
    "accepted":           ["in_progress", "rejected"],
}

# Story state machine (simpler than task)
#
# DEAD COPY, AND SAYING SO IS THE POINT. Nothing imports either name from
# this module -- `story_transitions.py` takes the real table from
# `app/models/story.py`, which has `awaiting_merge` and this does not.
# Left in place rather than deleted because removing a public name is a
# bigger change than labelling it, but anyone editing THIS table to change
# behaviour is editing the wrong file.
STORY_STATES = ["proposed", "accepted", "in_progress", "blocked", "done", "rejected"]

STORY_VALID_TRANSITIONS: dict[str, list[str]] = {
    "proposed":     ["accepted", "rejected"],
    "accepted":     ["in_progress", "rejected"],
    "in_progress":  ["blocked", "done"],
    "blocked":      ["in_progress", "done"],
    "done":         [],
    "rejected":     [],
}


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    assigned_to: str = ""          # human uid OR agent template ID
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    category: Literal["task", "story", "subtask", "milestone", "decision", "review"] = "task"
    due_at: Optional[datetime] = None
    estimated_effort: str = ""     # e.g., "2 hours", "1 day"
    depends_on: list[str] = []     # task IDs
    requires_resources: list[str] = []   # goal_resource IDs needed (read)
    produces_resources: list[str] = []   # goal_resource IDs produced (write)
    approval_required: bool = False
    template_id: str = ""          # agent template to instantiate
    parent_task_id: str = ""       # parent task for subtask hierarchy
    story_id: str = ""             # parent story (from stories table)
    acceptance_criteria: str = ""  # what "done" means


class Task(TaskCreate):
    id: str
    goal_id: str
    state: TaskState = "pending"
    instance_id: str = ""          # runtime instance (set on dispatch)
    source_thread_id: str = ""     # SHCA thread that created this task
    blocked_reason: str = ""       # why task is blocked
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_summary: str = ""       # brief summary of what was accomplished
    output_resources: list[str] = []  # goal_resource IDs produced by this task
    created_by: str
    created_at: Optional[datetime] = None
    status: Literal["active", "deleted"] = "active"


class TaskEvent(BaseModel):
    from_state: TaskState
    to_state: TaskState
    actor: str                    # uid or agent_id
    timestamp: Optional[datetime] = None
    note: str = ""
