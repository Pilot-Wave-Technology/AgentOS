"""/goal resume — resume a paused goal from Slack.

Wraps app.services.execution_control.resume(). Execution control is a
PLATFORM-OPERATOR capability: only platform admins/editors may resume —
goal-level admins (goal.manager) deliberately have no path here, matching the
web UI (admin_controls.py).

Only reachable as a /goal subcommand (see goal.py) — there is no standalone
/resume slash command registered with the Slack App.
"""
import logging

from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user,
)
from app.roles import SystemRole

_log = logging.getLogger(__name__)


async def handle(
    slack_user_id: str, channel_id: str, team_id: str,
    command: str = "",
) -> str:
    if not command:
        from app.channels.slack.commands.registry import _cmd_prefix
        command = _cmd_prefix("goal")
    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return (
            f"Run /{command} resume from a goal channel, orchestrator channel, or assistant channel."
        )

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."

    # Platform-operator only — goal.manager has no execution-control path.
    system_role = await pool.fetchval(
        "SELECT system_role FROM users WHERE uid = $1", user_uid,
    )
    if system_role not in SystemRole.PRIVILEGED:  # platform admin or editor
        _log.warning(
            "Slack /goal resume denied: uid=%s system_role=%s goal=%s",
            user_uid, system_role, goal_id,
        )
        return "Only platform admins or editors can resume execution."

    from app.services.execution_control import resume
    was_paused = await resume("goal", goal_id)
    if not was_paused:
        return "Goal execution is not paused — nothing to resume."

    _log.info("Slack /goal resume: goal=%s by=%s", goal_id, user_uid)
    return "▶️ Goal execution resumed."
