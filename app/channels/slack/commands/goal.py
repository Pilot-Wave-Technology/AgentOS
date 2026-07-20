"""/goal — goal summary, status, members, invitations, and session control.

  /goal                           summary of the goal linked to this channel
  /goal status [audit]            health dashboard; audit for recent activity
  /goal members                   list goal members and their roles
  /goal invite <email> [role]     add an existing control-panel user to this goal
  /goal remove <email>            remove a member from this goal
  /goal pause [reason]            pause task dispatch for this goal (platform admin/editor only)
  /goal resume                    resume a paused goal (platform admin/editor only)
  /goal refresh                   reset the gateway session for this channel

Creating a NEW goal is intentionally web-only — use the web UI.
"""
import logging

from app.auth import get_goal_role
from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user, display_name,
)
from app.roles import SystemRole, GoalRole

_log = logging.getLogger(__name__)


async def handle(
    args: str, channel_id: str, team_id: str,
    slack_user_id: str = "", bot_token: str = "",
    response_url: str = "",
    command: str = "",
) -> str:
    cmd = command or "goal"
    pool = get_pool()
    goal_id, resolved_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    effective_token = bot_token or resolved_token

    parts = (args or "").split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("help", "--help", "-h"):
        from app.channels.slack.commands.registry import render_command_help
        return render_command_help("goal")

    if sub == "status":
        from app.channels.slack.commands.status import handle as _h
        return await _h(rest, channel_id, team_id, slack_user_id=slack_user_id, command=cmd)

    if sub == "pause":
        from app.channels.slack.commands.pause import handle as _h
        return await _h(rest, slack_user_id, channel_id, team_id, command=cmd)

    if sub == "resume":
        from app.channels.slack.commands.resume import handle as _h
        return await _h(slack_user_id, channel_id, team_id, command=cmd)

    if sub == "refresh":
        from app.channels.slack.commands.refresh import handle as _h
        return await _h(slack_user_id, channel_id, team_id, response_url, command=cmd)

    if sub in ("members", "invite", "remove"):
        if not goal_id:
            return f"Run /{cmd} members|invite|remove from a channel linked to a goal."
        if sub == "members":
            return await _list_members(goal_id, slack_user_id, effective_token, pool)
        if sub == "remove":
            return await _remove_member(goal_id, rest, slack_user_id, effective_token, pool, cmd=cmd)
        return await _invite_member(goal_id, rest, slack_user_id, effective_token, pool, cmd=cmd)

    if not goal_id:
        from app.channels.slack.config import list_goals_for_workspace
        goals = await list_goals_for_workspace(team_id, pool)
        if not goals:
            return "No goals are linked to this workspace yet."
        lines = ["Goals linked to this workspace:"]
        for g in goals[:20]:
            lines.append(f"• *{g['goal_name']}* — channel <#{g['goal_channel_id']}>")
        return "\n".join(lines)

    # Bare `/goal` summary — same membership requirement as the web dashboard
    # (goal_dashboard in app/routers/goals.py): must be a goal member.
    requester_uid = await resolve_user(slack_user_id, effective_token, pool)
    if not requester_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."
    if await get_goal_role(requester_uid, goal_id) is None:
        return "You're not a member of this goal."

    goal = await pool.fetchrow(
        "SELECT name, description FROM goals WHERE id = $1", goal_id,
    )
    if not goal:
        return "Goal not found."

    state_counts = await pool.fetch("""
        SELECT state, count(*) AS c FROM stories
         WHERE goal_id = $1 AND state NOT IN ('cancelled', 'rejected')
         GROUP BY state
    """, goal_id)
    counts = {r["state"]: r["c"] for r in state_counts}

    in_progress = await pool.fetch("""
        SELECT id, title, assigned_to FROM stories
         WHERE goal_id = $1 AND state = 'in_progress'
         ORDER BY started_at DESC LIMIT 10
    """, goal_id)
    blocked = await pool.fetch("""
        SELECT id, title, blocked_reason FROM stories
         WHERE goal_id = $1 AND state = 'blocked'
         ORDER BY updated_at DESC LIMIT 5
    """, goal_id)

    lines = [f"*Goal:* {goal['name']}"]
    if goal.get("description"):
        lines.append(f"_{goal['description'][:200]}_")
    lines.append("")
    lines.append(
        "Stories — "
        + " · ".join(
            f"{state}: *{counts.get(state, 0)}*"
            for state in ("proposed", "accepted", "in_progress", "blocked", "done")
        )
    )
    if in_progress:
        lines.append("\n*In progress:*")
        for s in in_progress:
            who = await display_name(pool, s.get("assigned_to", "")) or "unassigned"
            lines.append(f"• {s['title']} — {who} ({s['id'][:8]})")
    if blocked:
        lines.append("\n*Blocked:*")
        for s in blocked:
            reason = (s.get("blocked_reason") or "")[:120]
            lines.append(f"• {s['title']} — {reason or 'no reason given'} ({s['id'][:8]})")

    return "\n".join(lines)


async def _list_members(goal_id: str, slack_user_id: str, bot_token: str, pool) -> str:
    """List members + emails. Manager-only — matches the web members page
    (goal_members_page in app/routers/goals.py), which requires goal.manager
    or platform.admin rather than plain membership."""
    requester_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not requester_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."
    requester_role = await get_goal_role(requester_uid, goal_id)
    is_sys_admin = await pool.fetchval(
        "SELECT system_role = 'admin' FROM users WHERE uid = $1", requester_uid,
    )
    if requester_role != GoalRole.GOAL_ADMIN and not is_sys_admin:
        return "Only goal admins can list members."

    rows = await pool.fetch(
        """SELECT gm.uid, gm.role, u.display_name, u.email
             FROM goal_members gm
             JOIN users u ON u.uid = gm.uid
            WHERE gm.goal_id = $1
            ORDER BY CASE gm.role WHEN 'admin' THEN 0 WHEN 'member' THEN 1 ELSE 2 END,
                     u.display_name""",
        goal_id,
    )
    if not rows:
        return "This goal has no members yet."
    lines = [f"*Members ({len(rows)}):*"]
    for r in rows:
        who = r["display_name"] or r["email"] or r["uid"]
        lines.append(f"  • {who} — *{r['role']}*")
    _log.info("Slack /goal members: goal=%s count=%d", goal_id, len(rows))
    return "\n".join(lines)


async def _invite_member(
    goal_id: str, rest: str, slack_user_id: str, bot_token: str, pool,
    cmd: str = "goal",
) -> str:
    """Add an existing control-panel user to this goal by email. Admin-only."""
    tokens = rest.split()
    if not tokens:
        return f"Usage: /{cmd} invite <email> [admin|member|viewer]"
    raw_email = tokens[0].strip()
    # Slack auto-formats emails as <mailto:user@example.com|user@example.com> — unwrap it
    if raw_email.startswith("<mailto:"):
        raw_email = raw_email[len("<mailto:"):].split("|")[0].split(">")[0]
    email = raw_email.lower()
    role = tokens[1].lower() if len(tokens) > 1 else GoalRole.GOAL_MEMBER
    if role not in (*GoalRole.ALL, "viewer"):
        return "Role must be one of: admin, member, viewer."

    inviter_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not inviter_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."

    from app.auth import get_goal_role
    inviter_role = await get_goal_role(inviter_uid, goal_id)
    is_sys_admin = await pool.fetchval(
        "SELECT system_role = 'admin' FROM users WHERE uid = $1", inviter_uid,
    )
    if inviter_role != GoalRole.GOAL_ADMIN and not is_sys_admin:
        return "Only goal admins can invite members."

    target = await pool.fetchrow(
        "SELECT uid, actor_id FROM users WHERE email = $1", email,
    )
    if not target:
        return (
            f"No control-panel user found with email `{email[:60]}`. "
            "They must sign up in AgentOS first, then you can invite them."
        )

    inviter_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", inviter_uid,
    )
    from app.services.goal_membership import add_goal_member
    ok, message = await add_goal_member(
        goal_id, target["uid"], role, inviter_uid, inviter_actor_id,
    )
    _log.info(
        "Slack /goal invite: goal=%s email=%s role=%s by=%s ok=%s",
        goal_id, email, role, inviter_uid, ok,
    )
    return ("✅ " if ok else "⚠️ ") + message


async def _remove_member(
    goal_id: str, rest: str, slack_user_id: str, bot_token: str, pool,
    cmd: str = "goal",
) -> str:
    """Remove a member from this goal by email.

    No platform.admin bypass — unlike _invite_member/_list_members above
    (a separate, pre-existing inconsistency not being fixed here), removal
    follows the same rule as the web UI's add_member/update_member_role:
    a platform.admin must actually be a member to act.
    """
    tokens = rest.split()
    if not tokens:
        return f"Usage: /{cmd} remove <email>"
    raw_email = tokens[0].strip()
    if raw_email.startswith("<mailto:"):
        raw_email = raw_email[len("<mailto:"):].split("|")[0].split(">")[0]
    email = raw_email.lower()

    remover_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not remover_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."

    from app.auth import get_goal_role
    remover_role = await get_goal_role(remover_uid, goal_id)
    if remover_role != GoalRole.GOAL_ADMIN:
        return "Only goal admins can remove members."

    target = await pool.fetchrow(
        "SELECT uid FROM users WHERE email = $1", email,
    )
    if not target:
        return f"No control-panel user found with email `{email[:60]}`."

    remover_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", remover_uid,
    )
    from app.services.goal_membership import remove_goal_member
    result = await remove_goal_member(goal_id, target["uid"], remover_uid, remover_actor_id)
    _log.info(
        "Slack /goal remove: goal=%s email=%s by=%s ok=%s",
        goal_id, email, remover_uid, result.get("ok"),
    )
    if result.get("status") == "needs_reassignment":
        stories = ", ".join(s["title"] for s in result.get("stories", []))
        return (
            f"⚠️ {email} has {len(result.get('stories', []))} story(ies) not yet started "
            f"that need reassignment first: {stories}. Use the web UI's Members page to "
            f"reassign and remove — that flow isn't available from Slack yet."
        )
    return ("✅ " if result.get("ok") else "⚠️ ") + result.get("message", "")