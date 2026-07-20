"""/memory — search your SHCA's memory for this goal from Slack.

Usage:
  /memory <query>    full-text search across ledger decisions, tasks, and agent memories

Searches are scoped to the current goal first, then widens to all goals the
SHCA has been active in.  Returns the top 5 results.
"""
import logging

from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user,
)

_log = logging.getLogger(__name__)


async def handle(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    command: str = "",
) -> str:
    cmd = command or "memory"
    if not args:
        return f"Usage: /{cmd} <query>  (search your Assistant's memory for this goal)"

    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return f"Run /{cmd} from a goal channel, orchestrator channel, or assistant channel."

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."

    # Resolve this user's SHCA agent id in this goal.
    # search_memory uses agent_id to look up agent_contexts, so we need
    # the agent's own id (not template_id).
    shca_row = await pool.fetchrow("""
        SELECT a.id AS agent_id
          FROM agents a
          JOIN agent_contexts ac ON ac.agent_id = a.id
          JOIN users u ON u.actor_id = a.owner_actor_id
         WHERE ac.goal_id = $1
           AND u.uid = $2
           AND a.type = 'SHCA'
           AND a.status = 'active'
         ORDER BY a.created_at DESC LIMIT 1
    """, goal_id, user_uid)

    if not shca_row:
        return (
            "No assistant found for you in this goal. "
            "Start a conversation with your assistant first."
        )

    from app.memory import search_memory
    try:
        results = await search_memory(
            pool=pool,
            agent_template_id=shca_row["agent_id"],
            goal_id=goal_id,
            q=args.strip(),
            scope="all",
            max_results=5,
        )
    except Exception as exc:
        _log.error(
            "Slack /memory search failed: goal=%s uid=%s query=%r error=%s",
            goal_id, user_uid, args[:60], exc,
        )
        return f"Memory search failed: {exc}"

    if not results:
        return f"No memory found matching `{args[:60]}`."

    query_display = args[:60] + ("…" if len(args) > 60 else "")
    lines = [f"*Memory results for* `{query_display}`:"]
    for r in results[:5]:
        label = (r.get("type") or r.get("source", "")).replace("_", " ")
        content = (r.get("content") or r.get("description") or r.get("summary") or "")[:200]
        goal_name = r.get("goal_name", "")
        context = f" _{goal_name}_" if goal_name else ""
        lines.append(f"• *{label}*{context} — {content}")

    _log.info(
        "Slack /memory: goal=%s uid=%s query=%r results=%d",
        goal_id, user_uid, args[:60], len(results),
    )
    return "\n".join(lines)
