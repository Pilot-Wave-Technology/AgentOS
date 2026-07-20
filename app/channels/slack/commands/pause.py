"""/goal pause — pause goal execution from Slack.

Wraps app.services.execution_control.pause() so operators can freeze a goal
without opening the web UI. Execution control is a PLATFORM-OPERATOR capability:
only platform admins/editors may pause — goal-level admins (goal.manager)
deliberately have no path here, matching the web UI (admin_controls.py).

Only reachable as a /goal subcommand (see goal.py) — there is no standalone
/pause slash command registered with the Slack App.
"""
import logging

from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user,
)
from app.roles import SystemRole

_log = logging.getLogger(__name__)


async def handle(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    command: str = "",
) -> str:
    if not command:
        from app.channels.slack.commands.registry import _cmd_prefix
        command = _cmd_prefix("goal")
    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return (
            f"Run /{command} pause from a goal channel, orchestrator channel, or assistant channel."
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
            "Slack /goal pause denied: uid=%s system_role=%s goal=%s",
            user_uid, system_role, goal_id,
        )
        return "Only platform admins or editors can pause execution."

    from app.services.execution_control import pause, get_pause
    existing = await get_pause("goal", goal_id)
    if existing:
        return (
            f"Goal is already paused — reason: _{existing.get('reason') or 'none'}_\n"
            f"Use /{command} resume to clear it."
        )

    reason = args.strip() or "Paused from Slack"
    await pause("goal", goal_id, reason, user_uid)

    _log.info(
        "Slack /goal pause: goal=%s by=%s reason=%s", goal_id, user_uid, reason,
    )
    return (
        f"⏸️ Goal execution paused.\nReason: _{reason}_\n"
        f"Use /{command} resume to resume."
    )
