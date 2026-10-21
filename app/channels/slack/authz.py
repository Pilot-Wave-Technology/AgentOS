"""Is this Slack user allowed to act on this thing?

THE WEB ROUTES ALWAYS ASKED; THE SLACK BUTTONS MOSTLY DID NOT. Every
mutating web route does `get_goal_role(uid, pid) is None -> 403`. On the
Slack side exactly one branch did it -- `recover_*` -- and the story
control, diff and card families resolved the clicking user and then
acted. The action id carries the story/approval/task id, so a mapped
Slack user could approve a plan, approve-and-PUSH a diff, or replan a
story in a goal they have no role on, by clicking a button whose id
came from anywhere.

`chat.py:1550-1559` already carries the comment explaining why this
matters, next to the same query written the safe way. This module is
that rule, once, so the next button family cannot be written without it.

Each resolver answers the same question in a different id-space, and
each returns the goal id so the caller can log what it refused.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# What the user sees. Deliberately identical across id-spaces: telling
# somebody *which* goal they are not a member of is itself a leak.
DENIED = "You don't have access to this story's goal."


async def goal_for_story(pool, story_id: str) -> str | None:
    if not story_id:
        return None
    return await pool.fetchval(
        "SELECT goal_id FROM stories WHERE id = $1", story_id)


async def goal_for_task(pool, task_id: str) -> str | None:
    if not task_id:
        return None
    return await pool.fetchval(
        "SELECT goal_id FROM tasks WHERE id = $1", task_id)


async def goal_for_approval(pool, approval_id: str) -> str | None:
    """Approvals carry no goal of their own; the story they hang off does."""
    if not approval_id:
        return None
    return await pool.fetchval(
        "SELECT s.goal_id FROM approvals a "
        "JOIN stories s ON s.id = a.story_id WHERE a.id = $1",
        approval_id,
    )


async def may_act_on_goal(user_uid: str, goal_id: str | None) -> bool:
    """Membership of the goal, with a missing goal treated as a refusal.

    `goal_id` is None when the subject does not exist -- a story id that
    was never real, or one from a goal that has been hard-deleted. Both
    are refusals rather than errors: the caller has no business knowing
    which.
    """
    if not user_uid or not goal_id:
        return False
    from app.auth import get_goal_role  # noqa: PLC0415 -- avoids an import cycle
    return await get_goal_role(user_uid, goal_id) is not None


async def authorize(pool, *, user_uid: str, kind: str, subject_id: str) -> bool:
    """`kind` is one of story / task / approval. True means go ahead.

    A refusal is logged at WARNING with the subject, because somebody
    pressing a button for a goal they are not in is either a bug in our
    rendering or somebody trying it on, and both are worth seeing.
    """
    resolver = {
        "story": goal_for_story,
        "task": goal_for_task,
        "approval": goal_for_approval,
    }.get(kind)
    if resolver is None:
        raise ValueError(f"unknown authorize kind: {kind!r}")

    goal_id = await resolver(pool, subject_id)
    allowed = await may_act_on_goal(user_uid, goal_id)
    if not allowed:
        _log.warning(
            "Slack authz: REFUSED uid=%s %s=%s goal=%s",
            user_uid, kind, subject_id, goal_id or "-",
        )
    return allowed
