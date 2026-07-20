"""DEPRECATED — superseded by app/channels/slack/commands/.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Slack slash command handlers.

Supported commands:
  /work <story name or id>  — validate assignment + start working on the story
                              (same end state as an @project-assistant mention)
  /goal                     — summary of the goal linked to this channel
  /story [id]               — list stories or show a specific story's detail
  /task [id]                — show status for a specific task
  /done <id> [feedback...]  — mark a story or root task as done. Optional
                              [--force] for non-active states. Feedback is
                              recorded in the ledger and on tasks.result if
                              the task has no result yet.
  /human-review <root-task-id> [feedback...]
                              — clear a pending Human-review checkpoint under
                              this root task with optional feedback. The root
                              auto-completes once all subtasks finish.
  /delete <task-id>         — hard-delete a task and its incomplete subtasks
                              (status='deleted'). Reversible only by DB edit.
  /refresh                  — reset the gateway session for whichever agent
                              owns the current Slack channel. Story channel →
                              that story's SHCA. Goal channel or orchestrator
                              channel → the GO. Mirrors the Refresh button on
                              the control panel agent page: archives the
                              transcript and re-provisions RESOURCES.md /
                              IDENTITY.md / MEMORY.md on the next message.

Commands are handled inline (the HTTP response becomes the ephemeral message
Slack shows the user). For /work, which may take several seconds to create
the channel and spin up the SHCA, we kick the heavy work off as a background
task and ack immediately — the final result is posted via response_url.
"""
import asyncio
import logging
from typing import Optional

import httpx

from app.database import get_pool
from app.roles import SystemRole, GoalRole
from app.openclaw import make_goal_session_key
from app.slack.config import (
    get_goal_by_channel,
    get_goal_by_orch_channel,
    list_goals_for_workspace,
)
from app.slack.thread_map import auto_map_user_by_email, get_user_uid

_log = logging.getLogger(__name__)


# ── Slash command timeout pattern ────────────────────────────────────────────
#
# Slack times out slash-command HTTP responses at 3 seconds. Any handler
# that touches the gateway (cleanup_parent_task, on_task_state_changed,
# gateway_admin.reset_session, etc.) will routinely blow that budget.
#
# Pattern for any handler with gateway-bound side effects:
#   1. Do fast validation + DB writes synchronously (Postgres is in-VPC,
#      single-digit-ms; the user-facing ack is truthful by the time we
#      return).
#   2. asyncio.create_task(_run_followup(response_url, label, coro))
#      for everything else.
#   3. Return the success message immediately.
#
# `_run_followup` posts a follow-up ephemeral message via response_url
# if the background coroutine raises — that's how errors surface back
# to the user past the 3s ack window.

async def _post_followup(response_url: str, text: str) -> None:
    """Post an ephemeral follow-up to a slash command's response_url.
    Slack keeps response_url valid for 30 minutes after the command."""
    if not response_url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(response_url, json={
                "response_type": "ephemeral",
                "replace_original": False,
                "text": text,
            })
    except Exception as exc:
        _log.warning("Slack response_url post failed: %s", exc)


async def _run_followup(response_url: str, label: str, coro) -> None:
    """Await `coro` and surface any exception via response_url.

    `label` is used in the error message so the user knows which step
    failed (e.g. "post-review cleanup", "task cleanup", "session reset").
    Errors are also logged with traceback. This never raises."""
    try:
        await coro
    except Exception as exc:
        _log.error(
            "Slack followup '%s' failed: %s", label, exc, exc_info=True,
        )
        await _post_followup(
            response_url,
            f"⚠️ {label} hit an error after the initial ack: {exc}",
        )


async def handle_slack_command(
    command: str,
    text: str,
    user_id: str,
    channel_id: str,
    team_id: str,
    response_url: str = "",
) -> str:
    """Dispatch a slash command. Returns an ephemeral message body."""
    cmd = (command or "").strip().lower().lstrip("/")
    # Strip env suffix so /goal-prod and /goal-dev both dispatch to "goal".
    # Lets multiple Slack apps (one per Cloud Run env) coexist in the same
    # workspace without slash-command name collisions.
    for _suffix in ("-prod", "-dev"):
        if cmd.endswith(_suffix):
            cmd = cmd[: -len(_suffix)]
            break
    # Slack auto-formats backtick-wrapped text as inline code and passes
    # the literal backticks through. Same for asterisks (bold) and
    # underscores (italics) when users copy-paste from a card. Strip
    # these formatting characters so /command id works the same as
    # /command id. Asterisks/underscores aren't meaningful in any of
    # our id formats so global removal is safe.
    args = (text or "").replace("`", "").strip()

    _log.info(
        "Slack command: /%s %r (user=%s channel=%s team=%s)",
        cmd, args[:80], user_id, channel_id, team_id,
    )

    if cmd == "work":
        return await _cmd_work(args, user_id, channel_id, team_id, response_url)
    if cmd == "goal":
        return await _cmd_goal(args, channel_id, team_id)
    if cmd == "story":
        return await _cmd_story(args, channel_id, team_id)
    if cmd == "task":
        return await _cmd_task(args, channel_id, team_id)
    if cmd == "done":
        return await _cmd_done(args, user_id, channel_id, team_id, response_url)
    if cmd in ("human-review", "human_review", "humanreview"):
        return await _cmd_human_review(args, user_id, channel_id, team_id, response_url)
    if cmd == "delete":
        return await _cmd_delete(args, user_id, channel_id, team_id, response_url)
    if cmd == "refresh":
        return await _cmd_refresh(user_id, channel_id, team_id, response_url)

    return (
        f"/{cmd} is not a recognised command. "
        f"Try /work, /goal, /story, /task, /done, /human-review, "
        f"/delete, or /refresh."
    )


# ── /work ────────────────────────────────────────────────────────────────────

async def _cmd_work(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    response_url: str,
) -> str:
    """Start working on a story. Mirrors the @assistant-mention flow."""
    if not args:
        return "Usage: /work <story title or short id>"

    config = await _resolve_goal_config(channel_id, team_id)
    if not config:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            "Please run /work from the goal channel."
        )

    user_uid = await _resolve_user(slack_user_id, team_id, config["bot_token"])
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control panel "
            "with the same email first, then try again."
        )

    # Kick off the heavy work in the background; respond to Slack immediately.
    asyncio.create_task(_run_work_on_story_async(
        team_id=team_id,
        config=config,
        slack_user=slack_user_id,
        user_uid=user_uid,
        text=args,
        response_url=response_url,
    ))
    return f"Working on it — finding *{args[:60]}* and setting up your channel…"


async def _run_work_on_story_async(
    team_id: str, config: dict, slack_user: str, user_uid: str,
    text: str, response_url: str,
) -> None:
    """Run the full _handle_work_on_story flow and report back via response_url."""
    try:
        from app.slack.inbound import _handle_work_on_story
        await _handle_work_on_story(
            team_id=team_id, config=config,
            slack_user=slack_user, user_uid=user_uid,
            text=text, bot_token=config["bot_token"],
        )
        # _handle_work_on_story already posts its own ephemeral messages
        # (success with the channel link OR error). We don't need to duplicate
        # those on response_url, but we can send a no-op update so the "Working
        # on it…" ack doesn't linger.
    except Exception as exc:
        _log.error("Slash /work async flow failed: %s", exc, exc_info=True)
        if response_url:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(response_url, json={
                        "response_type": "ephemeral",
                        "replace_original": False,
                        "text": f"Something went wrong: {exc}",
                    })
            except Exception:
                pass


# ── /goal ────────────────────────────────────────────────────────────────────

async def _cmd_goal(args: str, channel_id: str, team_id: str) -> str:
    """Show a compact goal status summary."""
    goal_id, _ = await _resolve_goal_from_any_channel(channel_id, team_id)
    if not goal_id:
        # If not in a goal-linked channel, list goals available in this workspace
        goals = await list_goals_for_workspace(team_id)
        if not goals:
            return "No goals are linked to this workspace yet."
        lines = ["Goals linked to this workspace:"]
        for g in goals[:20]:
            lines.append(
                f"• *{g['goal_name']}* — channel <#{g['goal_channel_id']}>"
            )
        return "\n".join(lines)

    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT name, description FROM goals WHERE id = $1", goal_id,
    )
    if not goal:
        return "Goal not found."

    # Story counts by state
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
            who = await _display_name(pool, s.get("assigned_to", "")) or "unassigned"
            lines.append(f"• {s['title']} — {who} ({s['id'][:8]})")

    if blocked:
        lines.append("\n*Blocked:*")
        for s in blocked:
            reason = (s.get("blocked_reason") or "")[:120]
            lines.append(f"• {s['title']} — {reason or 'no reason given'} ({s['id'][:8]})")

    return "\n".join(lines)


# ── /story ───────────────────────────────────────────────────────────────────

async def _cmd_story(args: str, channel_id: str, team_id: str) -> str:
    """List stories (no args) or show one story's detail (with an id prefix)."""
    goal_id, _ = await _resolve_goal_from_any_channel(channel_id, team_id)
    if not goal_id:
        return (
            "Run /story from a goal channel, an orchestrator channel, "
            "a story channel, or your assistant channel."
        )

    pool = get_pool()

    if args:
        row = await pool.fetchrow("""
            SELECT id, title, description, state, assigned_to, priority,
                   blocked_reason, progress_summary, created_at
              FROM stories
             WHERE goal_id = $1 AND (id = $2 OR id LIKE $3)
             ORDER BY (id = $2) DESC, created_at DESC
             LIMIT 1
        """, goal_id, args.strip(), f"{args.strip()[:8]}%")
        if not row:
            return f"No story found for {args[:30]}."
        who = await _display_name(pool, row.get("assigned_to", "")) or "unassigned"
        lines = [
            f"*{row['title']}* ({row['id'][:8]})",
            f"state: *{row['state']}* · priority: *{row['priority']}* · assigned to: *{who}*",
        ]
        if row.get("description"):
            lines.append(f"_{row['description'][:500]}_")
        if row.get("blocked_reason"):
            lines.append(f"⚠️ _Blocked:_ {row['blocked_reason'][:300]}")
        if row.get("progress_summary"):
            lines.append(f"📌 _Progress:_ {row['progress_summary'][:500]}")
        return "\n".join(lines)

    rows = await pool.fetch("""
        SELECT id, title, state, assigned_to FROM stories
         WHERE goal_id = $1 AND state NOT IN ('cancelled', 'rejected')
         ORDER BY CASE state
                    WHEN 'in_progress' THEN 0
                    WHEN 'blocked' THEN 1
                    WHEN 'accepted' THEN 2
                    WHEN 'proposed' THEN 3
                    WHEN 'done' THEN 4
                    ELSE 5 END, created_at DESC
         LIMIT 20
    """, goal_id)
    if not rows:
        return "No stories in this goal yet."
    lines = ["*Stories:*"]
    for r in rows:
        who = await _display_name(pool, r.get("assigned_to", "")) or "unassigned"
        lines.append(f"• {r['id'][:8]} *{r['state']}* · {r['title']} — {who}")
    return "\n".join(lines)


# ── /task ────────────────────────────────────────────────────────────────────

async def _cmd_task(args: str, channel_id: str, team_id: str) -> str:
    """Show a task's current status by id (or id prefix)."""
    if not args:
        return "Usage: /task <id or id prefix>"

    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT id, goal_id, title, state, template_id, assigned_to,
               blocked_reason, created_at
          FROM tasks
         WHERE id = $1 OR id LIKE $2
         ORDER BY (id = $1) DESC, created_at DESC LIMIT 1
    """, args.strip(), f"{args.strip()[:8]}%")
    if not row:
        return f"No task found for {args[:30]}."

    lines = [
        f"*{row['title']}* ({row['id'][:8]})",
        f"state: *{row['state']}* · template: {row['template_id']}",
    ]
    if row.get("blocked_reason"):
        lines.append(f"⚠️ {row['blocked_reason'][:300]}")
    return "\n".join(lines)


# ── /done ───────────────────────────────────────────────────────────────────

async def _mark_story_done(
    pool, story, user_uid: str, force: bool = False, response_url: str = "",
) -> str:
    """Apply the same logic as the control-panel UI's Done button on a
    story. Validates the transition + assignment, releases produced-
    resource locks, fires GO + Slack notifications.

    force=True lets a goal admin close a story they're not assigned
    to (e.g. unblocking a stuck story whose owner is unavailable).
    """
    from app.models.story import STORY_VALID_TRANSITIONS

    sid = story["id"]
    pid = story["goal_id"]
    old_state = story["state"]
    assigned_to = story.get("assigned_to") or ""

    # Engine merge stories land via the PR + detection, never a manual/forced done —
    # a forced done would falsely satisfy the merge-dependency for downstream consumers.
    if (story.get("story_kind") or "code") == "merge":
        return (
            f"Story {sid[:8]} is a Git Engine merge story — it lands when the PR is "
            f"merged. Use Approve/Cancel in the Git Engine view, not /done."
        )

    # Transition check.
    if "done" not in STORY_VALID_TRANSITIONS.get(old_state, []):
        return (
            f"Story {sid[:8]} is in state {old_state} — can't go directly "
            f"to done from there. Valid next states: "
            f"{', '.join(STORY_VALID_TRANSITIONS.get(old_state, [])) or '(none)'}."
        )

    # Authorisation: assignee, OR an admin with --force.
    if assigned_to and assigned_to != user_uid:
        if not force:
            return (
                f"Story {sid[:8]} is assigned to someone else. If you're "
                f"the goal admin and need to force-close it, run "
                f"/done {sid[:8]} --force."
            )
        from app.auth import get_goal_role
        role = await get_goal_role(user_uid, pid)
        if role != GoalRole.GOAL_ADMIN:
            is_sys_admin = await pool.fetchval(
                "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
            )
            if not is_sys_admin:
                return "Only goal admins can force-close someone else's story."
    elif not assigned_to:
        # Unassigned story — only admins can close (otherwise anyone could).
        from app.auth import get_goal_role
        role = await get_goal_role(user_uid, pid)
        if role != GoalRole.GOAL_ADMIN:
            is_sys_admin = await pool.fetchval(
                "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
            )
            if not is_sys_admin:
                return f"Story {sid[:8]} has no assignee — only an admin can close it."

    # Apply state change. Mirror the UPDATE in routers/stories.py
    # story_state_change(action='done').
    # Clear active_thread_id on terminal transition so the SHCA cleanup
    # watchdog's stories→agent_instances join doesn't mis-pick this
    # finished story when a new one comes in on the same thread.
    await pool.execute(
        "UPDATE stories SET state = 'done', completed_at = now(), "
        "active_thread_id = NULL, updated_at = now() "
        "WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    # Release locks on produced resources.
    await pool.execute("""
        UPDATE goal_resources
           SET locked_by = NULL, locked_for = NULL, locked_at = NULL
         WHERE locked_for = $1
    """, sid)

    _log.info(
        "Slack /done story: goal=%s story=%s %s → done force=%s by uid=%s",
        pid, sid, old_state, force, user_uid,
    )

    # GO + Slack notifications round-trip the gateway. Background.
    asyncio.create_task(_run_followup(
        response_url, "Post-story-done notifications",
        _story_done_followup_async(
            pid=pid, sid=sid, old_state=old_state,
            story_title=story["title"], user_uid=user_uid,
        ),
    ))
    return f"✅ Story {sid[:8]} *{story['title']}* marked done."


async def _story_done_followup_async(
    pid: str, sid: str, old_state: str, story_title: str, user_uid: str,
) -> None:
    """Background follow-up for /done on a story. Raises on failure."""
    from app.services import go_events
    await go_events.on_task_state_changed(pid, sid, old_state, "done", user_uid)
    try:
        from app.slack.lifecycle import on_story_state_changed
    except ImportError:
        return
    await on_story_state_changed(
        pid, sid, old_state, "done", story_title=story_title,
    )


async def _cmd_done(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    response_url: str = "",
) -> str:
    """Mark a story OR (root) task as done, optionally with feedback.

    /done <id> [feedback...] resolves the id against stories first
    (most common Slack use-case from the goal channel), then falls
    back to tasks.

    Stories: must be in state in_progress or blocked (per
    STORY_VALID_TRANSITIONS) and assigned to the caller. Releases locks
    on produced resources. Notifies GO + Slack via the same path the
    UI Done button uses.

    Tasks (admin-only): transitions the task to done (skipping the
    approved intermediate). Feedback is stored on tasks.result
    if the task has no result yet, and always recorded in the ledger
    event reason.
      • Already-terminal tasks (done/cancelled/failed) → no-op refusal.
      • awaiting_approval with no children = plan-approval pending.
        Refused with a hint to use the plan card; --force overrides.
      • --force completes any task from any state.

    For clearing a pending Human-review checkpoint, use
    /human-review <root-task-id> [feedback...] instead.
    """
    if not args:
        return (
            "Usage: /done <id> [feedback...]  "
            "(use --force to force-complete regardless of state)"
        )

    parts = args.strip().split()
    force = "--force" in parts
    non_flag = [p for p in parts if p and not p.startswith("--")]
    arg_id = non_flag[0] if non_flag else ""
    feedback = " ".join(non_flag[1:]).strip()
    if not arg_id:
        return (
            "Usage: /done <id> [feedback...]  "
            "(use --force to force-complete regardless of state)"
        )

    goal_id, bot_token = await _resolve_goal_from_any_channel(channel_id, team_id)
    if not goal_id:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            "Run /done from a goal channel, an orchestrator channel, a "
            "story channel, or your assistant channel."
        )

    user_uid = await _resolve_user(slack_user_id, team_id, bot_token)
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control "
            "panel first, then try again."
        )

    pool = get_pool()

    # 1. Try as a story id first. Stories are the user-facing unit and
    # this command is most commonly invoked from the goal channel.
    story = await pool.fetchrow("""
        SELECT id, goal_id, title, state, assigned_to, story_kind
          FROM stories
         WHERE goal_id = $1
           AND (id = $2 OR id LIKE $3)
         ORDER BY (id = $2) DESC, created_at DESC LIMIT 1
    """, goal_id, arg_id, f"{arg_id[:8]}%")
    if story:
        return await _mark_story_done(
            pool, story, user_uid, force=force, response_url=response_url,
        )

    # 2. Fall back to task id.
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

    # Authorisation — same rule as the /approve HTTP endpoint.
    from app.auth import get_goal_role
    role = await get_goal_role(user_uid, task["goal_id"])
    if role != GoalRole.GOAL_ADMIN:
        is_sys_admin = await pool.fetchval(
            "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
        )
        if not is_sys_admin:
            return "Only goal admins can mark a task done."

    current_state = task["state"]
    template_id = (task.get("template_id") or "").lower()

    # /done is for marking the (root) task as done. Use /human-review
    # to clear a pending human-review checkpoint with feedback. We
    # always transition to 'done' (skipping the 'approved' intermediate)
    # since /done in Slack means "this is finished, full stop".
    #
    #   already terminal (done/cancelled/failed)         → no-op refusal
    #   awaiting_approval with no children (plan)        → reject (use card)
    #   --force                                          → done from any state
    #   else                                             → done
    if current_state in ("done", "cancelled", "failed"):
        return (
            f"Task {task['id'][:8]} is already in state {current_state} — "
            "nothing to do."
        )

    if current_state == "awaiting_approval" and not force:
        has_children = await pool.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM tasks
                 WHERE parent_task_id = $1 AND status = 'active'
            )
        """, task["id"])
        if not has_children:
            return (
                f"Task {task['id'][:8]} is awaiting *plan* approval. "
                "Approve via the plan card in the Assistant channel — /done "
                "would skip subtask creation."
            )

    new_state = "done"
    note_prefix = (
        "Force-completed from Slack" if force else "Marked done from Slack"
    )

    # Apply the state change with proper ledger emission. Mirrors the
    # core of approve_task in app/routers/tasks.py — we keep the SQL
    # local rather than calling the route handler directly because the
    # HTTP route uses Firebase auth, not Slack auth.
    from app.ledger.schema import TASK_STATUS_CHANGED
    from app.ledger.payloads import task_status_changed_payload
    from app.task_controller import (
        emit_task_event, get_task_correlation_id, get_last_event_id,
    )

    note = note_prefix + (f": {feedback}" if feedback else "")

    user_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", user_uid,
    )
    corr_id = await get_task_correlation_id(pool, task["id"])
    last_evt = await get_last_event_id(pool, task["id"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            await emit_task_event(
                conn,
                event_type=TASK_STATUS_CHANGED,
                task_id=task["id"], goal_id=task["goal_id"],
                payload=task_status_changed_payload(
                    previous_status=current_state, new_status=new_state,
                    reason=note,
                ),
                actor_id=user_actor_id, actor_type="human",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=note,
            )
            await conn.execute(
                "UPDATE tasks SET state = $1 WHERE id = $2",
                new_state, task["id"],
            )
            # If feedback was provided and the task has no result yet
            # (e.g. user is closing a task that never ran an agent),
            # store the feedback as the result so it shows in the UI.
            if feedback and not task.get("result"):
                await conn.execute(
                    "UPDATE tasks SET result = $1 WHERE id = $2",
                    feedback[:32000], task["id"],
                )

    _log.info(
        "Slack /done: goal=%s task=%s %s → %s force=%s by uid=%s",
        task["goal_id"], task["id"], current_state, new_state, force, user_uid,
    )

    # Cleanup + downstream notifications round-trip the gateway and can
    # blow the 3s slot. Run in the background; errors surface via
    # response_url after the ack.
    asyncio.create_task(_run_followup(
        response_url, "Post-done cleanup",
        _done_followup_async(
            task_id=task["id"], goal_id=task["goal_id"],
            from_state=current_state, to_state=new_state, user_uid=user_uid,
        ),
    ))

    suffix = f"\n> {feedback}" if feedback else ""
    return f"✅ Task {task['id'][:8]} *{task['title']}* marked done.{suffix}"


async def _done_followup_async(
    task_id: str, goal_id: str, from_state: str, to_state: str, user_uid: str,
) -> None:
    """Background follow-up for /done. Raises on first failure;
    _run_followup catches and surfaces it."""
    from app.task_controller import cleanup_parent_task, task_controller
    from app.services.go_events import on_task_state_changed

    await cleanup_parent_task(task_id)
    await task_controller.trigger()
    await on_task_state_changed(goal_id, task_id, from_state, to_state, user_uid)


# ── /human-review ───────────────────────────────────────────────────────────

async def _cmd_human_review(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    response_url: str = "",
) -> str:
    """Clear a pending Human-review checkpoint under a root task.

    Usage: /human-review <root-task-id> [feedback...]

    The persistent-worker session key is "agent:{instance}:task-{ROOT}",
    so SHCA cards and logs always reference the ROOT task id — not the
    individual subtask id. This command takes the root id the user
    sees in cards and resolves the actual human-review subtask under
    it (template_id='human', state='blocked'), then transitions it to
    'done' with the feedback.

    Feedback is required-ish — if omitted, "approved" is used so the
    review still has a discoverable artefact.

    Also accepts the human-review's own task id directly (idempotent
    convenience for users who paste it from the UI).
    """
    if not args:
        return (
            "Usage: /human-review <root-task-id> [approve|reject] [feedback...]  "
            "— clear the pending Human review checkpoint. Defaults to approve."
        )

    # Parse: <id> [approve|reject] [feedback...]
    # First token is the task id. Optional second token is the action keyword
    # (approve|reject); if missing, defaults to approve. The rest is feedback.
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
            # Second token isn't an action — treat everything after id as feedback
            feedback = " ".join(raw_parts[1:]).strip()

    goal_id, bot_token = await _resolve_goal_from_any_channel(channel_id, team_id)
    if not goal_id:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            "Run /human-review from a goal channel, an orchestrator channel, "
            "a story channel, or your assistant channel."
        )

    user_uid = await _resolve_user(slack_user_id, team_id, bot_token)
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control "
            "panel first, then try again."
        )

    pool = get_pool()

    # Match the targeted task. The user typically pastes the ROOT id
    # (what they see in SHCA cards), but accept the human-review's own
    # id too — both resolve to the same human-review subtask below.
    target = await pool.fetchrow("""
        SELECT id, goal_id, title, state, parent_task_id, template_id, assigned_to
          FROM tasks
         WHERE goal_id = $1 AND status = 'active'
           AND (id = $2 OR id LIKE $3)
         ORDER BY (id = $2) DESC, created_at DESC LIMIT 1
    """, goal_id, arg_id, f"{arg_id[:8]}%")
    if not target:
        return f"No active task in this goal matched {arg_id[:30]}."

    # Resolve to the human-review subtask. Accept several forms because
    # the planner's output isn't always perfectly normalised:
    #   • template_id (or assigned_to) == 'human' (case-insensitive)
    #   • title matches "human review%" — fallback for plans that named
    #     the step but assigned a non-human template
    target_template = (target.get("template_id") or "").lower()
    target_assignee = (target.get("assigned_to") or "").lower()
    if target_template == "human" or target_assignee == "human":
        hr = target
    else:
        # Walk anywhere in the descendant tree — the human-review row
        # is normally a direct child but a re-plan can nest it deeper.
        hr = await pool.fetchrow("""
            WITH RECURSIVE descendants AS (
                SELECT id, goal_id, title, state, parent_task_id,
                       template_id, assigned_to, status, created_at
                  FROM tasks WHERE parent_task_id = $1
                UNION ALL
                SELECT t.id, t.goal_id, t.title, t.state, t.parent_task_id,
                       t.template_id, t.assigned_to, t.status, t.created_at
                  FROM tasks t
                  JOIN descendants d ON t.parent_task_id = d.id
            )
            SELECT id, goal_id, title, state, parent_task_id
              FROM descendants
             WHERE status = 'active'
               AND (LOWER(COALESCE(template_id, '')) = 'human'
                    OR LOWER(COALESCE(assigned_to, '')) = 'human'
                    OR LOWER(title) LIKE 'human review%')
             ORDER BY (state = 'blocked') DESC, created_at DESC
             LIMIT 1
        """, target["id"])
        if not hr:
            # Show what subtasks DO exist so the user can see what the
            # planner actually produced.
            kids = await pool.fetch("""
                SELECT id, title, state, template_id, assigned_to
                  FROM tasks
                 WHERE parent_task_id = $1 AND status = 'active'
                 ORDER BY created_at
            """, target["id"])
            if not kids:
                return (
                    f"Task {target['id'][:8]} *{target['title']}* has no "
                    "subtasks yet — the planner may still be running, or "
                    "the plan hasn't been approved."
                )
            lines = [
                f"Task {target['id'][:8]} *{target['title']}* has no "
                "Human-review checkpoint I could match. Subtasks:",
            ]
            for k in kids:
                tag = k.get("template_id") or k.get("assigned_to") or "—"
                lines.append(
                    f"• {k['id'][:8]} *{k['title']}* — {tag} ({k['state']})"
                )
            return "\n".join(lines)

    if hr["state"] == "done":
        return (
            f"Human review {hr['id'][:8]} *{hr['title']}* is already done."
        )
    if hr["state"] not in ("blocked", "awaiting_approval"):
        return (
            f"Human review {hr['id'][:8]} is in state {hr['state']}, "
            "not awaiting human action — refusing to short-circuit."
        )

    # Authorisation — admin only, same rule as /done.
    from app.auth import get_goal_role
    role = await get_goal_role(user_uid, hr["goal_id"])
    if role != GoalRole.GOAL_ADMIN:
        is_sys_admin = await pool.fetchval(
            "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
        )
        if not is_sys_admin:
            return "Only goal admins can clear a Human-review checkpoint."

    # Delegate to the unified completion path so UI and Slack share the
    # same logic: review subtask → done, main task → done (approve) or
    # rejected (reject), ledger events emitted on both, GO notified.
    #
    # complete_human_review does gateway round-trips (cleanup IAs,
    # on_task_state_changed) which can exceed Slack's 3s ack window, so
    # we schedule it as a background task and ack the slash command now.
    from app.task_controller import complete_human_review

    asyncio.create_task(_run_followup(
        response_url, "Human review completion",
        complete_human_review(
            review_task_id=hr["id"],
            action=action,
            feedback=feedback,
            actor_uid=user_uid,
        ),
    ))

    _log.info(
        "Slack /human-review queued: goal=%s review=%s root=%s action=%s uid=%s feedback_len=%d",
        hr["goal_id"], hr["id"], hr.get("parent_task_id") or "-",
        action, user_uid, len(feedback),
    )

    main_state_label = "done" if action == "approve" else "rejected"
    icon = "✅" if action == "approve" else "❌"
    suffix = f"\n> {feedback}" if feedback else ""
    return (
        f"{icon} Human review *{hr['title']}* — {action}d. "
        f"Main task → `{main_state_label}`.{suffix}"
    )


async def _human_review_followup_async(
    review_id: str, parent_task_id: str, goal_id: str, user_uid: str,
) -> None:
    """Background follow-up for /human-review. Raises on first failure;
    _run_followup catches and surfaces it back to the user.

    Order: cleanup gateway agents → bubble parent completion → trigger
    controller tick → wake GO/SHCA. The cleanup is housekeeping and
    rarely matters if it fails, but a failure here usually indicates
    the gateway is down — surfacing it is honest.
    """
    from app.task_controller import (
        check_parent_completion, cleanup_parent_task, task_controller,
    )
    from app.services.go_events import on_task_state_changed

    await cleanup_parent_task(review_id)
    if parent_task_id:
        await check_parent_completion(parent_task_id)
    await task_controller.trigger()
    await on_task_state_changed(
        goal_id, review_id, "blocked", "done", user_uid,
    )


# ── /delete ─────────────────────────────────────────────────────────────────

async def _cmd_delete(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    response_url: str = "",
) -> str:
    """Hard-delete a task and its incomplete subtasks (status='deleted').

    Mirrors the /goals/{pid}/tasks/{tid}/delete route in app/routers/tasks.py
    but invoked via Slack auth rather than Firebase. Cancelled subtasks
    are marked status='deleted'; completed/approved subtasks keep their
    immutable state but are still hidden via status='deleted' on the
    task row.
    """
    if not args:
        return "Usage: /delete <task-id>"

    task_arg = args.strip().split()[0]

    goal_id, bot_token = await _resolve_goal_from_any_channel(channel_id, team_id)
    if not goal_id:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            "Run /delete from a goal channel, an orchestrator channel, a "
            "story channel, or your assistant channel."
        )

    user_uid = await _resolve_user(slack_user_id, team_id, bot_token)
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control "
            "panel first, then try again."
        )

    pool = get_pool()
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
        is_sys_admin = await pool.fetchval(
            "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
        )
        if not is_sys_admin:
            return "Only goal admins can delete tasks."

    # Walk the task tree and collect all descendants. Same approach as
    # the HTTP delete endpoint — recursive CTE keeps it one round-trip.
    tree_rows = await pool.fetch("""
        WITH RECURSIVE descendants AS (
          SELECT id, parent_task_id, state, status FROM tasks WHERE id = $1
          UNION ALL
          SELECT t.id, t.parent_task_id, t.state, t.status
            FROM tasks t
            JOIN descendants d ON t.parent_task_id = d.id
           WHERE t.status = 'active'
        )
        SELECT id, state FROM descendants WHERE status = 'active'
    """, task["id"])
    tree_ids = [r["id"] for r in tree_rows]
    incomplete_ids = [
        r["id"] for r in tree_rows
        if r["state"] not in ("done", "approved", "cancelled")
    ]

    user_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", user_uid,
    )
    from app.ledger.schema import TASK_STATUS_CHANGED
    from app.ledger.payloads import task_status_changed_payload
    from app.task_controller import emit_task_event, get_task_correlation_id

    # Resolve correlation ids OUTSIDE the transaction (the helper takes
    # the pool, not a connection) — the values are stable for the
    # duration of this call.
    corr_ids = {
        tid: await get_task_correlation_id(pool, tid) for tid in incomplete_ids
    }

    from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Cancel any in-flight subtasks first (so their state is honest).
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
                await conn.execute(
                    "UPDATE tasks SET state = 'cancelled' WHERE id = $1",
                    tid,
                )
                await release_credentials_safe(tid, "legacy_slack_delete:tree_cancel")
            # Then mark the entire subtree status='deleted'.
            await conn.execute(
                "UPDATE tasks SET status = 'deleted' WHERE id = ANY($1::text[])",
                tree_ids,
            )

    _log.info(
        "Slack /delete: goal=%s task=%s tree_size=%d cancelled=%d by uid=%s",
        task["goal_id"], task["id"], len(tree_ids), len(incomplete_ids), user_uid,
    )

    # cleanup_parent_task tears down gateway agent instances — round-trips
    # the gateway, can be slow. The DB tree-delete above is already
    # committed, so the user-facing ack is true.
    async def _delete_cleanup():
        from app.task_controller import cleanup_parent_task
        await cleanup_parent_task(task["id"])
    asyncio.create_task(_run_followup(
        response_url, "Post-delete cleanup", _delete_cleanup(),
    ))

    return (
        f"🗑️ Deleted task {task['id'][:8]} *{task['title']}* "
        f"({len(tree_ids)} task{'s' if len(tree_ids) != 1 else ''} in tree, "
        f"{len(incomplete_ids)} cancelled in flight)."
    )


# ── /refresh ────────────────────────────────────────────────────────────────

async def _cmd_refresh(
    slack_user_id: str, channel_id: str, team_id: str,
    response_url: str = "",
) -> str:
    """Reset the gateway session for the agent owning this channel.

    Resolves which agent based on the channel:
      - Slack story channel → that story's SHCA per-conversation instance.
      - Per-user SHCA channel → the user's persistent SHCA for the goal.
      - Goal main channel or orchestrator channel → the goal's GO.

    Mirrors the Refresh button on the control panel agent detail page
    (POST /goals/{pid}/chat/{agent_id}/threads/{thread_id}/refresh in
    app/routers/chat.py): calls gateway_admin.reset_session to archive
    the current transcript on the gateway side, then evicts the
    in-memory _AgentSession so the next message triggers a fresh
    _provision_resources pass — that's what re-renders RESOURCES.md /
    IDENTITY.md / MEMORY.md from current DB state.
    """
    pool = get_pool()
    from app.openclaw import gateway_admin, session_manager
    from app.slack import story_channels as story_ch
    from app.slack import user_goal_channels as ugc

    # Resolve user — needed for goal-membership check below.
    bot_token = ""
    config = await _resolve_goal_config(channel_id, team_id)
    if config:
        bot_token = config.get("bot_token") or ""
    user_uid = await _resolve_user(slack_user_id, team_id, bot_token)
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control "
            "panel first, then try again."
        )

    # 1. Story channel? → refresh the SHCA.
    story_row = await story_ch.get_by_channel_id(channel_id)
    if story_row:
        goal_id = story_row["goal_id"]
        thread_id = story_row.get("thread_id") or ""
        agent_id = story_row.get("agent_id") or ""
        if not thread_id or not agent_id:
            return "This story channel isn't fully wired up yet (no thread or agent linked)."

        from app.auth import get_goal_role
        role = await get_goal_role(user_uid, goal_id)
        if role is None:
            return "You're not a member of this goal."

        story_title = story_row.get("story_title") or story_row.get("story_id") or "this story"
        label = f"Assistant for *{story_title}*"
        asyncio.create_task(_run_followup(
            response_url, "Refresh",
            _reset_session(
                gateway_admin, session_manager,
                goal_id=goal_id, agent_id=agent_id, thread_id=thread_id,
                label=label, user_uid=user_uid, response_url=response_url,
            ),
        ))
        return f"🔄 Refreshing {label}…"

    # 2. Per-user SHCA channel? → refresh that user's SHCA.
    ugc_row = await ugc.get_by_channel_id(channel_id)
    if ugc_row:
        goal_id = ugc_row["goal_id"]
        thread_id = ugc_row.get("thread_id") or ""
        agent_id = ugc_row.get("agent_id") or ""
        if not thread_id or not agent_id:
            return "This assistant channel isn't fully wired up yet (no thread or agent linked)."

        from app.auth import get_goal_role
        role = await get_goal_role(user_uid, goal_id)
        if role is None:
            return "You're not a member of this goal."

        label = f"your Assistant *{agent_id}*"
        asyncio.create_task(_run_followup(
            response_url, "Refresh",
            _reset_session(
                gateway_admin, session_manager,
                goal_id=goal_id, agent_id=agent_id, thread_id=thread_id,
                label=label, user_uid=user_uid, response_url=response_url,
            ),
        ))
        return f"🔄 Refreshing {label}…"

    # 3. Goal main channel or orchestrator channel? → refresh the GO.
    if config is None:
        return (
            "I couldn't tell which agent this channel belongs to. "
            "Run /refresh from a story channel, your assistant channel, "
            "the goal channel, or the orchestrator channel."
        )

    goal_id = config["goal_id"]
    from app.auth import get_goal_role
    role = await get_goal_role(user_uid, goal_id)
    if role is None:
        return "You're not a member of this goal."

    goal = await pool.fetchrow(
        "SELECT go_agent_id, go_thread_id, name FROM goals WHERE id = $1",
        goal_id,
    )
    if not goal or not goal.get("go_agent_id") or not goal.get("go_thread_id"):
        return "This goal doesn't have a Goal Orchestrator wired up."

    label = f"Goal Orchestrator for *{goal['name']}*"
    asyncio.create_task(_run_followup(
        response_url, "Refresh",
        _reset_session(
            gateway_admin, session_manager,
            goal_id=goal_id,
            agent_id=goal["go_agent_id"],
            thread_id=goal["go_thread_id"],
            label=label, user_uid=user_uid, response_url=response_url,
        ),
    ))
    return f"🔄 Refreshing {label}…"


async def _reset_session(
    gateway_admin, session_manager,
    *, goal_id: str, agent_id: str, thread_id: str, label: str, user_uid: str,
    response_url: str = "",
) -> None:
    """Common reset: gateway-side archive + in-memory eviction + ledger
    event. Posts the final result via response_url.

    Raises if the critical step (gateway reset_session) fails so
    _run_followup can surface the error. Ledger append + in-memory
    eviction are housekeeping; logged but not raised.
    """
    pool = get_pool()
    user_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", user_uid,
    )

    # Build the same session key send_to_gateway uses (pid lowercased
    # because the gateway canonicalises sessionKeys).
    session_key = make_goal_session_key(agent_id, goal_id, thread_id)
    # The gateway reset is THE point of /refresh — let any error propagate.
    await gateway_admin.reset_session(session_key)
    _log.info("Slack /refresh reset session: %s", session_key)

    # Ledger event for audit (same shape as the HTTP refresh route).
    # Soft-fail — we don't want a ledger blip to claim the refresh failed.
    try:
        from app.ledger.journal import append_event
        from app.ledger.schema import THREAD_REFRESHED
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    event_type=THREAD_REFRESHED,
                    payload={
                        "thread_id": thread_id,
                        "agent_id": agent_id,
                        "reason": "slack_/refresh",
                    },
                    actor_id=user_actor_id,
                    context_goal_id=goal_id,
                    context_conversation_id=thread_id,
                )
    except Exception as exc:
        _log.warning("Slack /refresh: ledger append failed: %s", exc)

    # Evict the in-memory session so the next message re-creates it →
    # _provision_resources fires → fresh RESOURCES.md/IDENTITY.md/MEMORY.md.
    # Soft-fail too; the gateway-side reset is what matters.
    try:
        session_manager.evict_thread(goal_id, agent_id, thread_id)
    except Exception as exc:
        _log.warning(
            "Slack /refresh: evict_thread failed (goal=%s agent=%s thread=%s): %s",
            goal_id, agent_id, thread_id[:12] if thread_id else "-", exc,
        )

    await _post_followup(
        response_url,
        f"🔄 Refreshed {label}. The next message will start a new session "
        f"with the latest RESOURCES.md / IDENTITY.md / MEMORY.md.",
    )


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _resolve_goal_config(channel_id: str, team_id: str) -> Optional[dict]:
    """Find the goal whose channel matches. Checks the main goal channel first,
    then the orchestrator channel (so /goal works from either)."""
    cfg = await get_goal_by_channel(team_id, channel_id)
    if cfg:
        return cfg
    return await get_goal_by_orch_channel(team_id, channel_id)


async def _resolve_goal_from_any_channel(
    channel_id: str, team_id: str,
) -> tuple[str, str]:
    """Resolve (goal_id, bot_token) from any of the goal's Slack surfaces.

    Tries in order:
      1. Main goal channel (slack_config.channel_id)
      2. Orchestrator channel (slack_config.orch_channel_id)
      3. Story channel (slack_story_channels.slack_channel_id)
      4. Per-user SHCA channel (slack_user_goal_channels.slack_channel_id)

    Story and SHCA channels don't carry their own bot_token; the goal-
    level slack_config row does. We always look the bot_token up from
    the goal-level config keyed by the resolved goal_id.

    Returns ("", "") if no surface matches — caller surfaces the
    "couldn't figure out which goal" error.
    """
    # 1 + 2: main goal or orchestrator channel — these already attach
    # the bot_token directly.
    cfg = await _resolve_goal_config(channel_id, team_id)
    if cfg:
        return cfg["goal_id"], cfg.get("bot_token") or ""

    from app.slack.config import get_goal_slack_config

    # 3: story channel. Look it up, then fetch the goal-level config
    # to pick up the workspace bot_token.
    from app.slack import story_channels as story_ch
    story_row = await story_ch.get_by_channel_id(channel_id)
    if story_row:
        goal_id = story_row.get("goal_id") or ""
        if goal_id:
            goal_cfg = await get_goal_slack_config(goal_id)
            return goal_id, (goal_cfg or {}).get("bot_token") or ""

    # 4: per-user SHCA channel. Lets commands run from a user's
    # personal goal-channel attach to the same goal as the GO.
    from app.slack import user_goal_channels as ugc
    ugc_row = await ugc.get_by_channel_id(channel_id)
    if ugc_row:
        goal_id = ugc_row.get("goal_id") or ""
        if goal_id:
            goal_cfg = await get_goal_slack_config(goal_id)
            return goal_id, (goal_cfg or {}).get("bot_token") or ""

    return "", ""


async def _resolve_user(slack_user_id: str, team_id: str, bot_token: str) -> str:
    if not slack_user_id or not team_id:
        return ""
    uid = await get_user_uid(slack_user_id, team_id)
    if uid:
        return uid
    try:
        from slack_sdk import WebClient
        client = WebClient(token=bot_token)
        info = client.users_info(user=slack_user_id)
        profile = (info.get("user") or {}).get("profile", {})
        email = profile.get("email", "")
        display_name = profile.get("display_name") or profile.get("real_name", "")
        if email:
            return await auto_map_user_by_email(
                slack_user_id, team_id, email, display_name,
            ) or ""
    except Exception as exc:
        _log.warning("Failed to resolve Slack user %s: %s", slack_user_id, exc)
    return ""


async def _display_name(pool, uid: str) -> str:
    if not uid:
        return ""
    try:
        return await pool.fetchval(
            "SELECT display_name FROM users WHERE uid = $1", uid,
        ) or ""
    except Exception:
        return ""
