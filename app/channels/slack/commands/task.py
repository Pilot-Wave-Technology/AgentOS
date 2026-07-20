"""/task — list, show status, or delete tasks.

Subcommands:
  /task list <story-id>   list all tasks for a story
  /task <id>              show a task's current status
  /task delete <id>       hard-delete a task tree (admin)

Force-kill is intentionally web-only — use Admin Controls in the control panel.
"""
import logging

from app.database import get_pool
from app.channels.slack.commands._helpers import resolve_goal_from_any_channel

_log = logging.getLogger(__name__)


async def handle(
    args: str, channel_id: str, team_id: str,
    slack_user_id: str = "", response_url: str = "",
    command: str = "",
) -> str:
    cmd = command or "task"
    if not args:
        from app.channels.slack.commands.registry import render_command_help
        return render_command_help("task")

    parts = args.strip().split(None, 1)
    sub = parts[0].lower()

    if sub in ("help", "--help", "-h"):
        from app.channels.slack.commands.registry import render_command_help
        return render_command_help("task")
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "kill":
        return "Force-kill is managed from the control panel (Admin Controls → Kill Switch)."

    if sub == "list":
        if not rest:
            return f"Usage: /{cmd} list <story-id>"
        return await _list_tasks(rest, channel_id, team_id, cmd=cmd)

    if sub == "delete":
        if not rest:
            return f"Usage: /{cmd} delete <task-id>"
        from app.channels.slack.commands.delete import handle as _h
        return await _h(rest, slack_user_id, channel_id, team_id, response_url, command=cmd)

    # Default: show task by id (sub is the id prefix)
    task_id = args.strip()
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT id, goal_id, title, state, template_id, assigned_to,
               blocked_reason, created_at
          FROM tasks
         WHERE id = $1 OR id LIKE $2
         ORDER BY (id = $1) DESC, created_at DESC LIMIT 1
    """, task_id, f"{task_id[:8]}%")
    if not row:
        return f"No task found for {args[:30]}."

    lines = [
        f"*{row['title']}* ({row['id'][:8]})",
        f"state: *{row['state']}* · template: {row['template_id']}",
    ]
    if row.get("blocked_reason"):
        lines.append(f"⚠️ {row['blocked_reason'][:300]}")
    return "\n".join(lines)


async def _list_tasks(story_prefix: str, channel_id: str, team_id: str,
                      cmd: str = "task") -> str:
    pool = get_pool()
    goal_id, _ = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return f"Run /{cmd} list from a goal channel, orchestrator channel, or assistant channel."

    story = await pool.fetchrow("""
        SELECT id, title FROM stories
         WHERE goal_id = $1 AND (id = $2 OR id LIKE $3)
         ORDER BY (id = $2) DESC, created_at DESC LIMIT 1
    """, goal_id, story_prefix, f"{story_prefix[:8]}%")
    if not story:
        return f"No story matched `{story_prefix[:16]}` in this goal."

    rows = await pool.fetch("""
        SELECT id, title, state, blocked_reason
          FROM tasks
         WHERE story_id = $1 AND status = 'active'
         ORDER BY CASE state
                    WHEN 'in_progress'      THEN 0
                    WHEN 'dispatching'      THEN 1
                    WHEN 'blocked'          THEN 2
                    WHEN 'awaiting_approval' THEN 3
                    WHEN 'ready'            THEN 4
                    WHEN 'pending'          THEN 5
                    WHEN 'done'             THEN 6
                    ELSE 7 END, created_at
         LIMIT 30
    """, story["id"])

    if not rows:
        return f"No tasks found for story *{story['title']}* (`{story['id'][:8]}`)."

    lines = [f"*Tasks for* _{story['title']}_ (`{story['id'][:8]}`):"]
    for r in rows:
        state_icon = {
            "in_progress": "🔵", "dispatching": "🔵", "awaiting_approval": "🟡",
            "blocked": "🔴", "done": "✅", "failed": "❌", "halted": "⛔",
        }.get(r["state"], "⚪")
        line = f"{state_icon} `{r['id'][:8]}` *{r['state']}* — {r['title']}"
        if r.get("blocked_reason"):
            line += f"\n  ⚠️ _{r['blocked_reason'][:120]}_"
        lines.append(line)

    _log.info("Slack /task list: goal=%s story=%s count=%d", goal_id, story["id"], len(rows))
    return "\n".join(lines)
