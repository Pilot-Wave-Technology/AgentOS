"""/story done — mark a story or root task as done, optionally with feedback.

Only reachable as a /story subcommand (see story.py) — there is no standalone
/done slash command registered with the Slack App.
"""
import asyncio
import logging

from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user,
    run_followup,
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
        command = _cmd_prefix("story")
    usage = (
        f"Usage: /{command} done <id> [feedback...]  "
        "(use --force to force-complete regardless of state)"
    )
    if not args:
        return usage

    parts = args.strip().split()
    force = "--force" in parts
    non_flag = [p for p in parts if p and not p.startswith("--")]
    arg_id = non_flag[0] if non_flag else ""
    feedback = " ".join(non_flag[1:]).strip()
    if not arg_id:
        return usage

    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            f"Run /{command} done from a goal channel, an orchestrator channel, a "
            "story channel, or your assistant channel."
        )

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control "
            "panel first, then try again."
        )

    from app.auth import get_goal_role as _get_goal_role_for_membership
    if await _get_goal_role_for_membership(user_uid, goal_id) is None:
        return "You're not a member of this goal."

    # Try story first
    story = await pool.fetchrow("""
        SELECT id, goal_id, title, state, assigned_to, story_kind
          FROM stories
         WHERE goal_id = $1 AND (id = $2 OR id LIKE $3)
         ORDER BY (id = $2) DESC, created_at DESC LIMIT 1
    """, goal_id, arg_id, f"{arg_id[:8]}%")
    if story:
        return await _mark_story_done(pool, story, user_uid, force=force, response_url=response_url, command=command)

    # Fall back to task
    task = await pool.fetchrow("""
        SELECT id, goal_id, title, state, parent_task_id, template_id,
               assigned_to, result
          FROM tasks
         WHERE goal_id = $1 AND status = 'active'
           AND (id = $2 OR id LIKE $3)
         ORDER BY (id = $2) DESC, created_at DESC LIMIT 1
    """, goal_id, arg_id, f"{arg_id[:8]}%")
    if not task:
        return f"No active story or task in this goal matched {arg_id[:30]}."

    from app.auth import get_goal_role
    role = await get_goal_role(user_uid, task["goal_id"])
    if role != GoalRole.GOAL_ADMIN:
        is_sys_admin = await pool.fetchval(
            "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
        )
        if not is_sys_admin:
            return "Only goal admins can mark a task done."

    current_state = task["state"]
    if current_state in ("done", "cancelled", "failed"):
        return (
            f"Task {task['id'][:8]} is already in state {current_state} — nothing to do."
        )
    if current_state == "awaiting_approval" and not force:
        has_children = await pool.fetchval("""
            SELECT EXISTS(SELECT 1 FROM tasks WHERE parent_task_id = $1 AND status = 'active')
        """, task["id"])
        if not has_children:
            return (
                f"Task {task['id'][:8]} is awaiting *plan* approval. "
                f"Approve via the plan card in the Assistant channel — /{command} done would skip subtask creation."
            )

    new_state = "done"
    note = ("Force-completed from Slack" if force else "Marked done from Slack") + (f": {feedback}" if feedback else "")

    from app.ledger.schema import TASK_STATUS_CHANGED
    from app.ledger.payloads import task_status_changed_payload
    from app.task_controller import emit_task_event, get_task_correlation_id, get_last_event_id

    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)
    corr_id = await get_task_correlation_id(pool, task["id"])
    last_evt = await get_last_event_id(pool, task["id"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            await emit_task_event(
                conn,
                event_type=TASK_STATUS_CHANGED,
                task_id=task["id"], goal_id=task["goal_id"],
                payload=task_status_changed_payload(
                    previous_status=current_state, new_status=new_state, reason=note,
                ),
                actor_id=user_actor_id, actor_type="human",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=note,
            )
            await conn.execute("UPDATE tasks SET state = $1 WHERE id = $2", new_state, task["id"])
            if feedback and not task.get("result"):
                await conn.execute("UPDATE tasks SET result = $1 WHERE id = $2", feedback[:32000], task["id"])

    _log.info(
        "Slack /story done: goal=%s task=%s %s → %s force=%s by uid=%s",
        task["goal_id"], task["id"], current_state, new_state, force, user_uid,
    )

    asyncio.create_task(run_followup(
        response_url, "Post-done cleanup",
        _done_followup_async(task["id"], task["goal_id"], current_state, new_state, user_uid),
    ))

    suffix = f"\n> {feedback}" if feedback else ""
    return f"✅ Task {task['id'][:8]} *{task['title']}* marked done.{suffix}"


async def _mark_story_done(
    pool, story, user_uid: str, force: bool = False, response_url: str = "",
    command: str = "story",
) -> str:
    """Delegate to the canonical story-transition service.

    Admin --force path: pre-claim the story so the service's assignment-ownership
    check permits the transition. The service owns resource locks, ledger events,
    GO notification, and channel lifecycle relay.

    TODO: extend apply_story_transition with a force=True param so the service
    owns the admin-override path entirely and the pre-claim here can be removed.
    """
    from app.services.story_transitions import apply_story_transition
    from app.auth import get_goal_role

    sid = story["id"]
    pid = story["goal_id"]
    assigned_to = story.get("assigned_to") or ""

    # Engine merge stories land via the PR + detection, never a manual done — a forced
    # done would falsely satisfy the merge-dependency for downstream consumers.
    if (story.get("story_kind") or "code") == "merge":
        return (
            f"Story {sid[:8]} is a Git Engine merge story — it lands when the PR is "
            f"merged. Use Approve/Cancel in the Git Engine view, not /{command} done."
        )

    # Admin force-close: bypass assignment ownership for admins.
    # We pre-claim the story so the service's assignment check passes.
    if force and (not assigned_to or assigned_to != user_uid):
        role = await get_goal_role(user_uid, pid)
        is_sys_admin = await pool.fetchval(
            "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
        )
        if role != GoalRole.GOAL_ADMIN and not is_sys_admin:
            return "Only goal admins can force-close this story."
        await pool.execute(
            "UPDATE stories SET assigned_to = $1, updated_at = now() WHERE id = $2 AND goal_id = $3",
            user_uid, sid, pid,
        )

    ok, reason = await apply_story_transition(pid, sid, "done", user_uid, pool)
    if not ok:
        if not force and "assigned to" in reason.lower():
            return (
                f"Story {sid[:8]} is assigned to someone else. "
                f"If you're the goal admin, run /{command} done {sid[:8]} --force."
            )
        return f"Could not mark story done: {reason}"

    _log.info(
        "Slack /story done: goal=%s story=%s → done force=%s uid=%s",
        pid, sid, force, user_uid,
    )
    return f"✅ Story {sid[:8]} *{story['title']}* marked done."


async def _done_followup_async(task_id: str, goal_id: str, from_state: str, to_state: str, user_uid: str) -> None:
    from app.task_controller import cleanup_parent_task, task_controller
    from app.services.go_events import on_task_state_changed
    await cleanup_parent_task(task_id)
    await task_controller.trigger()
    await on_task_state_changed(goal_id, task_id, from_state, to_state, user_uid)