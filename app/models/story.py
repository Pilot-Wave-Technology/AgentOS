"""Story model — human-level work items under a goal.

Stories are created by the GO agent, assigned to humans.
Humans import stories into SHCA conversations and execute them.
"""
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel


StoryState = Literal[
    "proposed",
    "accepted",
    "in_progress",
    "blocked",
    "done",
    "reviewed",
    "rejected",
    "cancelled",
]

STORY_VALID_TRANSITIONS: dict[str, list[str]] = {
    "proposed":     ["accepted", "rejected"],
    "accepted":     ["in_progress", "rejected"],
    "in_progress":  ["blocked", "done"],
    "blocked":      ["in_progress", "done"],
    # done -> reviewed is system-only (council final story review); humans never
    # drive it, so it is intentionally NOT exposed via the action map.
    "done":         ["reviewed"],
    "reviewed":     [],
    "rejected":     [],
    "cancelled":    [],
}


# Deterministic day/week label derived FROM story_weight — the one canonical
# size field. estimated_effort (free text) is retired from active use;
# anywhere the UI wants to show a human-friendly time estimate, look it up
# here instead of maintaining a second, separately-entered size field. This
# is the safe direction to derive in: a fixed lookup, not a text parser.
STORY_WEIGHT_EFFORT_LABELS: dict[int, str] = {
    1: "~1 day",
    2: "~1-2 days",
    3: "~3-5 days",
    5: "~1-2 weeks",
    8: "~2-4 weeks",
    13: "~1+ month",
}


# MOVED TO `kernel/story_kinds.py`, RE-EXPORTED HERE. The acceptance
# seam in `kernel/harness/park_resolve.py` needs this predicate to decide
# whether a finished run owes a review, and the kernel may not import
# from `app/`. Re-exporting rather than duplicating is the whole point:
# the docstring records what happened the last time two callers tested
# different conditions. Every existing import of this name still works.
from kernel.story_kinds import is_engine_git_story  # noqa: E402,F401


def story_weight_effort_label(story_weight: Optional[int]) -> str:
    """Human-friendly time estimate for a story_weight value, or a
    placeholder if the story hasn't been sized yet."""
    if story_weight is None:
        return "Not sized"
    return STORY_WEIGHT_EFFORT_LABELS.get(story_weight, f"{story_weight} pts")


class StoryCreate(BaseModel):
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    assigned_to: str = ""
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    estimated_effort: str = ""
    # Fibonacci story points (1,2,3,5,8,13) sizing complexity — the one
    # canonical size field (see STORY_WEIGHT_EFFORT_LABELS above). No enum
    # constraint at this layer or the DB layer (see migrations/story_weight.sql)
    # so the scale can change without a migration. Feeds the Taximeter's
    # Throughput category (app/services/taximeter/).
    story_weight: Optional[int] = None
    depends_on: list[str] = []


class Story(StoryCreate):
    id: str
    goal_id: str
    state: StoryState = "proposed"
    blocked_reason: str = ""
    result_summary: str = ""
    progress_summary: str = ""
    thread_id: str = ""
    created_by: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
