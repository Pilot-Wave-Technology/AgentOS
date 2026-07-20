"""/task delete — hard-delete a task and its incomplete subtasks.

Only reachable as a /task subcommand (see task.py) — there is no standalone
/delete slash command registered with the Slack App.
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
    if not command:
        from app.channels.slack.commands.registry import _cmd_prefix
        command = _cmd_prefix("task")
    if not args:
        return f"Usage: /{command} delete <task-id>"

    task_arg = args.strip().split()[0]

    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            f"Run /{command} delete from a goal channel, an orchestrator channel, a "
            "story channel, or your assistant channel."
        )

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in to the control panel first, then try again."

    task = await pool.fetchrow("""
        SELECT id, goal_id, title, state, parent_task_id
          FROM tasks
         WHERE goal_id = $1 AND status = 'active'
           AND (id = $2 OR id LIKE $3)
         ORDER BY (id = $2) DESC, created_at DESC LIMIT 1
    """, goal_id, task_arg, f"{task_arg[:8]}%")
    if not task:
        return f"No active task in this goal matched {task_arg[:30]}."

    from app.auth import get_goal_role
    role = await get_goal_role(user_uid, task["goal_id"])
    if role != GoalRole.GOAL_ADMIN:
        is_sys_admin = await pool.fetchval("SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid)
        if not is_sys_admin:
            return "Only goal admins can delete tasks."

    tree_rows = await pool.fetch("""
        WITH RECURSIVE descendants AS (
          SELECT id, parent_task_id, state, status FROM tasks WHERE id = $1
          UNION ALL
          SELECT t.id, t.parent_task_id, t.state, t.status
            FROM tasks t JOIN descendants d ON t.parent_task_id = d.id
           WHERE t.status = 'active'
        )
        SELECT id, state FROM descendants WHERE status = 'active'
    """, task["id"])
    tree_ids = [r["id"] for r in tree_rows]
    incomplete_ids = [r["id"] for r in tree_rows if r["state"] not in ("done", "approved", "cancelled")]

    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)
    from app.ledger.schema import TASK_STATUS_CHANGED
    from app.ledger.payloads import task_status_changed_payload
    from app.task_controller import emit_task_event, get_task_correlation_id

    corr_ids = {tid: await get_task_correlation_id(pool, tid) for tid in incomplete_ids}

    from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
    async with pool.acquire() as conn:
        async with conn.transaction():
            for tid in incomplete_ids:
                await emit_task_event(
                    conn,
                    event_type=TASK_STATUS_CHANGED,
                    task_id=tid, goal_id=task["goal_id"],
                    payload=task_status_changed_payload(
                        previous_status="*", new_status="cancelled",
                        reason="Parent deleted from Slack",
                    ),
                    actor_id=user_actor_id, actor_type="human",
                    correlation_id=corr_ids.get(tid),
                    summary="Cancelled (parent deleted)",
                )
                await conn.execute("UPDATE tasks SET state = 'cancelled' WHERE id = $1", tid)
                await release_credentials_safe(tid, "slack_delete:tree_cancel")
            await conn.execute(
                "UPDATE tasks SET status = 'deleted' WHERE id = ANY($1::text[])", tree_ids,
            )

    _log.info(
        "Slack /task delete: goal=%s task=%s tree=%d cancelled=%d by uid=%s",
        task["goal_id"], task["id"], len(tree_ids), len(incomplete_ids), user_uid,
    )

    async def _cleanup():
        from app.task_controller import cleanup_parent_task
        await cleanup_parent_task(task["id"])
    asyncio.create_task(run_followup(response_url, "Post-delete cleanup", _cleanup()))

    return (
        f"🗑️ Deleted task {task['id'][:8]} *{task['title']}* "
        f"({len(tree_ids)} task{'s' if len(tree_ids) != 1 else ''} in tree, "
        f"{len(incomplete_ids)} cancelled in flight)."
    )