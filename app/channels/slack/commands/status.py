"""/goal status — goal health dashboard and audit trail from Slack.

  /goal status          execution pause state, in-progress / blocked stories,
                        active tasks, and tasks awaiting approval
  /goal status audit    recent goal activity (ledger events) + token usage total

The audit view reads ledger_events (the canonical append-only store the web
monitor uses) so the Slack trail matches the web audit log.

Only reachable as a /goal subcommand (see goal.py) — there is no standalone
/status slash command registered with the Slack App.
"""
import logging

from app.database import get_pool
from app.channels.slack.commands._helpers import resolve_goal_from_any_channel

_log = logging.getLogger(__name__)


async def handle(
    args: str,
    channel_id: str,
    team_id: str,
    slack_user_id: str = "",
    command: str = "",
) -> str:
    if not command:
        from app.channels.slack.commands.registry import _cmd_prefix
        command = _cmd_prefix("goal")
    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return (
            f"Run /{command} status from a goal channel, orchestrator channel, or assistant channel."
        )

    # Membership check — only goal members (any role) may read goal status (D3).
    # Fails CLOSED: an unmapped Slack account or a lookup error must deny
    # access, not fall through to a full status/audit dump.
    from app.channels.slack.commands._helpers import resolve_user
    from app.auth import get_goal_role
    try:
        user_uid = await resolve_user(slack_user_id, bot_token, pool) if slack_user_id else None
    except Exception as exc:
        _log.warning("status: resolve_user failed for slack_user=%s: %s", slack_user_id, exc)
        user_uid = None
    if not user_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."
    if await get_goal_role(user_uid, goal_id) is None:
        return "You don't have access to this goal."

    if (args or "").strip().lower().startswith("audit"):
        return await _handle_audit(goal_id, pool)

    goal = await pool.fetchrow("SELECT name FROM goals WHERE id = $1", goal_id)

    # ── Pause state ────────────────────────────────────────────────────────────
    from app.services.execution_control import dispatch_block
    block = await dispatch_block(goal_id)

    # ── Stories ────────────────────────────────────────────────────────────────
    in_progress_stories = await pool.fetch("""
        SELECT id, title FROM stories
         WHERE goal_id = $1 AND state = 'in_progress'
         ORDER BY updated_at DESC LIMIT 5
    """, goal_id)
    blocked_stories = await pool.fetch("""
        SELECT id, title, blocked_reason FROM stories
         WHERE goal_id = $1 AND state = 'blocked'
         ORDER BY updated_at DESC LIMIT 5
    """, goal_id)

    # ── Tasks ──────────────────────────────────────────────────────────────────
    running_tasks = await pool.fetch("""
        SELECT id, title, state FROM tasks
         WHERE goal_id = $1 AND status = 'active'
           AND state IN ('dispatching', 'in_progress')
         ORDER BY started_at DESC LIMIT 5
    """, goal_id)
    approval_tasks = await pool.fetch("""
        SELECT id, title FROM tasks
         WHERE goal_id = $1 AND status = 'active'
           AND state = 'awaiting_approval'
         ORDER BY updated_at DESC LIMIT 5
    """, goal_id)

    # ── Assemble output ────────────────────────────────────────────────────────
    goal_name = (goal["name"] if goal else "") or goal_id[:8]
    lines = [f"*Goal status: {goal_name}*"]

    if block:
        lines.append(
            f"\n⏸️ *EXECUTION PAUSED* (scope: {block.scope})\n"
            f"Reason: _{block.reason or 'none given'}_\n"
            f"Use /{command} resume to resume."
        )
    else:
        lines.append("\n▶️ Execution running")

    if in_progress_stories:
        lines.append(f"\n*Stories in progress ({len(in_progress_stories)}):*")
        for s in in_progress_stories:
            lines.append(f"  • `{s['id'][:8]}` {s['title']}")

    if blocked_stories:
        lines.append(f"\n*Blocked stories ({len(blocked_stories)}):*")
        for s in blocked_stories:
            reason = (s.get("blocked_reason") or "")[:120]
            lines.append(
                f"  • `{s['id'][:8]}` {s['title']}"
                + (f" — _{reason}_" if reason else "")
            )

    if running_tasks:
        lines.append(f"\n*Running tasks ({len(running_tasks)}):*")
        for t in running_tasks:
            lines.append(f"  • `{t['id'][:8]}` {t['title']} [{t['state']}]")

    if approval_tasks:
        lines.append(
            f"\n*Awaiting your approval ({len(approval_tasks)}):*"
        )
        for t in approval_tasks:
            lines.append(f"  • `{t['id'][:8]}` {t['title']}")

    if not any([in_progress_stories, blocked_stories, running_tasks, approval_tasks]):
        lines.append("\nNo active stories or tasks.")

    _log.info("Slack /goal status: goal=%s", goal_id)
    return "\n".join(lines)


async def _handle_audit(goal_id: str, pool) -> str:
    """Recent goal activity from the ledger + a token-usage total.

    Mirrors the web monitor's audit query (ledger_events, goal-relevant event
    categories) so the Slack trail and the web audit log show the same events.
    """
    rows = await pool.fetch(
        """SELECT event_type, timestamp, actor_type, payload
             FROM ledger_events
            WHERE context_goal_id = $1
              AND (event_type LIKE 'task.%'
                   OR event_type LIKE 'story.%'
                   OR event_type LIKE 'goal.%'
                   OR event_type LIKE 'approval.%'
                   OR event_type LIKE 'escalation.%'
                   OR event_type LIKE 'execution.%')
            ORDER BY timestamp DESC
            LIMIT 12""",
        goal_id,
    )

    total_tokens = await pool.fetchval(
        """SELECT COALESCE(SUM(input_tokens + output_tokens
                               + cache_read_tokens + cache_write_tokens), 0)
             FROM token_calls
            WHERE goal_id::text = $1 AND record_type = 'call'""",
        goal_id,
    ) or 0

    lines = ["*Recent activity:*"]
    if not rows:
        lines.append("  (no recorded events yet)")
    for r in rows:
        ts = r["timestamp"].strftime("%m-%d %H:%M") if r["timestamp"] else ""
        summary = _summarise_event(r["event_type"], r["payload"])
        lines.append(f"  • `{ts}` {summary}")

    lines.append(f"\n*Tokens used (all agents):* {int(total_tokens):,}")
    _log.info("Slack /goal status audit: goal=%s events=%d tokens=%d", goal_id, len(rows), int(total_tokens))
    return "\n".join(lines)


def _summarise_event(event_type: str, payload) -> str:
    """One-line human summary for a ledger event (no agent/web coupling)."""
    import json
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    payload = payload or {}
    title = payload.get("title") or payload.get("story_title") or payload.get("task_title") or ""
    title = (title[:50]) if title else ""
    if event_type == "story.state_changed":
        return f"story {payload.get('from_state','?')}→{payload.get('to_state','?')} · {title}".strip()
    if event_type == "task.status_changed":
        return f"task {payload.get('previous_status','?')}→{payload.get('new_status','?')} · {title}".strip()
    suffix = f" · {title}" if title else ""
    return f"{event_type}{suffix}"
