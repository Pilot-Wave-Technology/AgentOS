"""/story review — clear a pending Human-review checkpoint under a root task.

Only reachable as a /story subcommand (see story.py) — there is no standalone
/human-review slash command registered with the Slack App.
"""
import asyncio
import logging

from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user, run_followup,
)
from app.roles import GoalRole

_log = logging.getLogger(__name__)


async def handle(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    response_url: str = "",
    command: str = "",
) -> str:
    """Usage: /<story> review <root-task-id> [approve|reject] [feedback...]"""
    if not command:
        from app.channels.slack.commands.registry import _cmd_prefix
        command = _cmd_prefix("story")
    if not args:
        return (
            f"Usage: /{command} review <root-task-id> [approve|reject] [feedback...]  "
            "— clear the pending Human review checkpoint. Defaults to approve."
        )

    raw_parts = args.strip().split(maxsplit=2)
    arg_id = raw_parts[0]
    action = "approve"
    feedback = ""
    if len(raw_parts) >= 2:
        second_lc = raw_parts[1].strip().lower()
        if second_lc in ("approve", "approved", "ok", "yes"):
            action = "approve"
            feedback = raw_parts[2].strip() if len(raw_parts) > 2 else ""
        elif second_lc in ("reject", "rejected", "no", "deny", "denied"):
            action = "reject"
            feedback = raw_parts[2].strip() if len(raw_parts) > 2 else ""
        else:
            feedback = " ".join(raw_parts[1:]).strip()

    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            f"Run /{command} review from a goal channel, an orchestrator channel, "
            "a story channel, or your assistant channel."
        )

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in to the control panel first, then try again."

    target = await pool.fetchrow("""
        SELECT id, goal_id, title, state, parent_task_id, template_id, assigned_to
          FROM tasks
         WHERE goal_id = $1 AND status = 'active'
           AND (id = $2 OR id LIKE $3)
         ORDER BY (id = $2) DESC, created_at DESC LIMIT 1
    """, goal_id, arg_id, f"{arg_id[:8]}%")
    if not target:
        return f"No active task in this goal matched {arg_id[:30]}."

    target_template = (target.get("template_id") or "").lower()
    target_assignee = (target.get("assigned_to") or "").lower()
    if target_template == "human" or target_assignee == "human":
        hr = target
    else:
        hr = await pool.fetchrow("""
            WITH RECURSIVE descendants AS (
                SELECT id, goal_id, title, state, parent_task_id,
                       template_id, assigned_to, status, created_at
                  FROM tasks WHERE parent_task_id = $1
                UNION ALL
                SELECT t.id, t.goal_id, t.title, t.state, t.parent_task_id,
                       t.template_id, t.assigned_to, t.status, t.created_at
                  FROM tasks t JOIN descendants d ON t.parent_task_id = d.id
            )
            SELECT id, goal_id, title, state, parent_task_id, assigned_to
              FROM descendants
             WHERE status = 'active'
               AND (LOWER(COALESCE(template_id, '')) = 'human'
                    OR LOWER(COALESCE(assigned_to, '')) = 'human'
                    OR LOWER(title) LIKE 'human review%')
             ORDER BY (state = 'blocked') DESC, created_at DESC
             LIMIT 1
        """, target["id"])
        if not hr:
            kids = await pool.fetch("""
                SELECT id, title, state, template_id, assigned_to
                  FROM tasks WHERE parent_task_id = $1 AND status = 'active' ORDER BY created_at
            """, target["id"])
            if not kids:
                return (
                    f"Task {target['id'][:8]} *{target['title']}* has no subtasks yet — "
                    "the planner may still be running, or the plan hasn't been approved."
                )
            lines = [
                f"Task {target['id'][:8]} *{target['title']}* has no Human-review checkpoint "
                "I could match. Subtasks:",
            ]
            for k in kids:
                tag = k.get("template_id") or k.get("assigned_to") or "—"
                lines.append(f"• {k['id'][:8]} *{k['title']}* — {tag} ({k['state']})")
            return "\n".join(lines)

    if hr["state"] == "done":
        return f"Human review {hr['id'][:8]} *{hr['title']}* is already done."
    if hr["state"] not in ("blocked", "awaiting_approval"):
        return (
            f"Human review {hr['id'][:8]} is in state {hr['state']}, "
            "not awaiting human action — refusing to short-circuit."
        )

    from app.auth import get_goal_role
    role = await get_goal_role(user_uid, hr["goal_id"])
    # Check the reviewed task's own assignee, not the human-review placeholder
    # subtask's — the placeholder's assigned_to is always the literal 'human'.
    if role != GoalRole.GOAL_ADMIN and (target.get("assigned_to") or "").lower() != user_uid.lower():
        return "Only the goal admin or the task's assignee can clear a Human-review checkpoint."

    from app.task_controller import complete_human_review
    asyncio.create_task(run_followup(
        response_url, "Human review completion",
        complete_human_review(review_task_id=hr["id"], action=action, feedback=feedback, actor_uid=user_uid),
    ))

    _log.info(
        "Slack /story review queued: goal=%s review=%s root=%s action=%s uid=%s",
        hr["goal_id"], hr["id"], hr.get("parent_task_id") or "-", action, user_uid,
    )

    main_state_label = "done" if action == "approve" else "rejected"
    icon = "✅" if action == "approve" else "❌"
    suffix = f"\n> {feedback}" if feedback else ""
    return (
        f"{icon} Human review *{hr['title']}* — {action}d. "
        f"Main task → `{main_state_label}`.{suffix}"
    )