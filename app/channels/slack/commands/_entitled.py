"""Is this the person who may start or cancel THIS story?

MEMBERSHIP OF THE GOAL IS NOT ENTITLEMENT TO THE STORY. Every command
checks `get_goal_role` already, which stops somebody acting on another
tenant's work -- but inside a goal it says yes to everybody. Starting a
story that is assigned to a colleague takes it out from under them;
cancelling one throws their work away.

TWO RULES, AND THEY DIFFER ON PURPOSE.

  STARTING is a claim. An unassigned story may be claimed by any
  member -- that is how work gets picked up. One already assigned to
  somebody else may not, because starting it would reassign it
  silently.

  CANCELLING is destructive, so it is narrower: the person holding the
  story, or a goal admin. A colleague cannot throw away work they are
  not doing.

AND THE REFUSAL SAYS WHO AND WHY. "You don't have access" would be a
lie here -- they do have access to the goal -- and it gives them
nothing to act on. Naming the holder turns a dead end into a
conversation.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


async def _display(pool, uid: str) -> str:
    if not uid:
        return "somebody"
    return await pool.fetchval(
        "SELECT display_name FROM users WHERE uid = $1", uid) or uid


async def may_start(pool, user_uid: str, story: dict) -> tuple[bool, str]:
    """`(ok, reason)` for starting. Unassigned is claimable; taken is not."""
    holder = (story.get("assigned_to") or "").strip()
    if not holder or holder == user_uid:
        return True, ""
    who = await _display(pool, holder)
    _log.info("Slack start REFUSED: uid=%s story=%s held by %s",
              user_uid, story.get("id"), holder)
    return False, (
        f"*{story.get('title') or story.get('id')}* is assigned to {who}. "
        f"Starting it would take it off them — ask them, or ask the Goal "
        f"Orchestrator to reassign it, and then try again."
    )


async def may_cancel(pool, user_uid: str, goal_id: str,
                     story: dict) -> tuple[bool, str]:
    """`(ok, reason)` for cancelling. The holder, or a goal admin."""
    holder = (story.get("assigned_to") or "").strip()
    if not holder or holder == user_uid:
        return True, ""

    from app.auth import get_goal_role
    from app.roles import GoalRole
    role = await get_goal_role(user_uid, goal_id)
    if role == GoalRole.GOAL_ADMIN:
        return True, ""

    who = await _display(pool, holder)
    _log.info("Slack cancel REFUSED: uid=%s story=%s held by %s role=%s",
              user_uid, story.get("id"), holder, role)
    return False, (
        f"*{story.get('title') or story.get('id')}* belongs to {who}, so it "
        f"is theirs to cancel. A goal admin can do it too — but cancelling "
        f"somebody else's story throws away whatever they have done, so it "
        f"is worth asking first."
    )
