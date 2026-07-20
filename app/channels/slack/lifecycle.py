"""Slack lifecycle event relay — self-contained implementation for the channel system.

Receives standard platform-agnostic lifecycle events from base.relay_lifecycle_event
and posts them to the appropriate Slack channels via platform_channel_bindings.

No imports from app.slack.* — fully self-contained in the new channel system.
Registered in channels/registry.py as _LIFECYCLE_RELAYS["slack"].

Standard event types handled:
  story.state_changed  → payload: {story_id, story_title, old_state, new_state}
  story.created        → payload: {story_id, story_title, assigned_to (optional)}
  goal.completed       → payload: {goal_name}
  goal.deleted         → payload: {goal_name} — archive all Slack channels
  orchestrator.message → payload: {content, username}
"""
import logging
import re
from typing import Optional

from app.roles import GoalRole, GOAL_ROLE_LABELS

_log = logging.getLogger(__name__)


def _channel_env_suffix() -> str:
    """Return a Slack-safe channel name suffix for the current environment.

    Uses SLACK_COMMAND_SUFFIX (e.g. 'dev', 'feature', 'prod') so channel names
    stay consistent with slash-command suffixes across environments. Returns an
    empty string when the suffix is not configured (local dev / single-env).
    """
    from app.config import get_settings
    suffix = (get_settings().slack_command_suffix or "").strip().lower()
    return f"-{suffix}" if suffix else ""


def _slugify_for_slack(text: str, max_len: int = 21) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "user"


def _channel_name_for(goal_id: str, user_display_name: str) -> str:
    env_suffix = _channel_env_suffix()
    goal_part = (goal_id or "").lower()[:30]
    user_budget = max(8, 80 - len(goal_part) - 1 - len(env_suffix))
    user_part = _slugify_for_slack(user_display_name, max_len=user_budget)
    return f"{goal_part}-{user_part}{env_suffix}"

_STATE_LABELS = {
    "proposed": "📋 Proposed",
    "accepted": "✅ Accepted",
    "in_progress": "🔄 In Progress",
    "blocked": "⚠️ Blocked",
    "done": "✅ Done",
    "cancelled": "🚫 Cancelled",
    "rejected": "❌ Rejected",
}


def _slugify(name: str, max_len: int = 80) -> str:
    """Convert a name to a Slack-safe channel slug (lowercase, a-z0-9-_)."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s\-_]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len] or "x"


async def relay_lifecycle_event(
    goal_id: str,
    event_type: str,
    payload: dict,
    thread_id: str | None = None,
) -> None:
    """Dispatch a standard lifecycle event to the relevant Slack channels."""
    try:
        if event_type == "story.state_changed":
            await _on_story_state_changed(goal_id, payload, thread_id)
        elif event_type == "story.created":
            await _on_story_created(goal_id, payload)
        elif event_type == "goal.completed":
            await _on_goal_completed(goal_id, payload)
        elif event_type == "goal.deleted":
            await _on_goal_deleted(goal_id, payload)
        elif event_type == "task.created":
            await _on_task_created(goal_id, payload)
        elif event_type in ("task.state_changed", "task.completed"):
            await _on_task_state_changed(goal_id, payload)
        elif event_type == "member.joined":
            await _on_member_joined(goal_id, payload)
        elif event_type == "member.left":
            await _on_member_left(goal_id, payload)
        elif event_type == "resource.registered":
            await _on_resource_registered(goal_id, payload)
        elif event_type == "orchestrator.message":
            content = payload.get("content", "")
            username = payload.get("username", "System")
            if content:
                from app.channels.slack.outbound import relay_orchestrator_message
                await relay_orchestrator_message(goal_id, content)
                _log.info(
                    "Slack lifecycle: orchestrator.message posted goal=%s username=%r len=%d",
                    goal_id, username, len(content),
                )
        else:
            _log.debug("Slack lifecycle: unhandled event_type=%s goal=%s", event_type, goal_id)
    except Exception as exc:
        _log.warning("Slack lifecycle relay error: event=%s goal=%s: %s", event_type, goal_id, exc)


async def _on_story_state_changed(
    goal_id: str,
    payload: dict,
    thread_id: str | None,
) -> None:
    """Post a story state-change notice to the user's Slack channel.

    Finds the user-goal channel binding whose current_story_id matches the story.
    Terminal transitions (done/cancelled/rejected) clear current_story_id and post
    a completion notice. Non-terminal posts a status update.

    Uses platform_channel_bindings.current_story_id for routing — the same field
    that slack_user_goal_channels.current_story_id served in the old system.
    """
    from app.channels.slack.config import get_integration_credentials
    from app.channels.slack.outbound import _post_message, _truncate_text
    from app.database import get_pool

    story_id = payload.get("story_id", "")
    story_title = payload.get("story_title") or f"`{story_id[:8]}`"
    old_state = payload.get("old_state", "")
    new_state = payload.get("new_state", "")
    state_label = _STATE_LABELS.get(new_state, new_state)

    _log.info(
        "Slack lifecycle: story.state_changed goal=%s story=%s %s→%s",
        goal_id, story_id[:8] if story_id else "", old_state, new_state,
    )

    pool = get_pool()

    if new_state in ("done", "cancelled", "rejected"):
        label_map = {
            "done": "✅ completed",
            "cancelled": "🚫 cancelled",
            "rejected": "❌ rejected",
        }
        msg = (
            f"Story *{story_title}* {label_map[new_state]}. "
            f"Ask the Goal Orchestrator to assign you the next story when you're ready "
            f"— this channel stays open for the goal."
        )
    else:
        msg = f"Story status update: *{story_title}* → {state_label}"

    # Find the user-goal binding whose current_story_id tracks this story
    binding_row = await pool.fetchrow("""
        SELECT pcb.external_channel_id, pcb.integration_id, pcb.actor_id,
               pcb.goal_id, pcb.actor_id
          FROM platform_channel_bindings pcb
          JOIN platform_integrations pi ON pi.id = pcb.integration_id
          JOIN goal_platform_integrations gpi ON gpi.integration_id = pcb.integration_id
                                             AND gpi.goal_id = pcb.goal_id
         WHERE pcb.goal_id        = $1
           AND pcb.current_story_id = $2
           AND pcb.actor_id        IS NOT NULL
           AND pcb.archived_at     IS NULL
           AND pi.platform         = 'slack'
           AND pi.status           = 'active'
           AND gpi.enabled         = true
         LIMIT 1
    """, goal_id, story_id)

    if binding_row:
        creds = await get_integration_credentials(str(binding_row["integration_id"]), pool)
        bot_token = (creds or {}).get("bot_token", "")
        if bot_token:
            try:
                await _post_message(bot_token, binding_row["external_channel_id"], msg, username="System")
                _log.info(
                    "Slack lifecycle: story state notice sent goal=%s story=%s channel=%s",
                    goal_id, story_id[:8] if story_id else "",
                    binding_row["external_channel_id"],
                )
            except Exception as exc:
                _log.warning(
                    "Slack lifecycle: story state notice failed goal=%s channel=%s: %s",
                    goal_id, binding_row["external_channel_id"], exc,
                )

        # Terminal transitions: clear current_story_id so this channel is free for the next story
        if new_state in ("done", "cancelled", "rejected") and story_id:
            await pool.execute(
                """UPDATE platform_channel_bindings
                      SET current_story_id = NULL
                    WHERE current_story_id = $1
                      AND goal_id = $2
                      AND archived_at IS NULL""",
                story_id, goal_id,
            )
            _log.info(
                "Slack lifecycle: cleared current_story_id=%s for goal=%s",
                story_id[:8] if story_id else "", goal_id,
            )
    elif new_state in ("accepted", "in_progress") and story_id:
        # Story became accepted/in_progress but no binding has current_story_id
        # pointing at it yet. Two cases:
        #   (a) the member already has a Slack channel from an earlier story in
        #       this goal - just repoint current_story_id at this one.
        #   (b) this is the member's first story in this goal - no binding row
        #       exists at all yet, so provision their personal channel now.
        assignee_row = await pool.fetchrow(
            """SELECT s.assigned_to AS uid, u.actor_id AS actor_id
                 FROM stories s
                 JOIN users u ON u.uid = s.assigned_to
                WHERE s.id = $1""",
            story_id,
        )
        if assignee_row:
            assigned_uid = assignee_row["uid"]
            assigned_actor_id = assignee_row["actor_id"]
            update_tag = await pool.execute(
                """UPDATE platform_channel_bindings
                      SET current_story_id = $1
                    WHERE goal_id        = $2
                      AND actor_id       = $3
                      AND archived_at    IS NULL
                      AND integration_id IN (
                          SELECT id FROM platform_integrations
                           WHERE platform = 'slack' AND status = 'active'
                      )""",
                story_id, goal_id, assigned_actor_id,
            )
            if update_tag == "UPDATE 0":
                # No existing binding for this member in this goal - their
                # personal Slack channel has never been created. Provision it.
                _log.info(
                    "Slack lifecycle: story %s -> %s, no existing binding for actor=%s goal=%s "
                    "- provisioning personal channel",
                    story_id[:8], new_state, str(assigned_actor_id)[:8], goal_id,
                )
                try:
                    from app.channels.slack.provisioner import _do_provision_member
                    await _do_provision_member(goal_id, assigned_uid, pool)
                except Exception as exc:
                    _log.warning(
                        "Slack lifecycle: personal channel creation failed uid=%s goal=%s: %s",
                        assigned_uid, goal_id, exc,
                    )
                return
            _log.info(
                "Slack lifecycle: set current_story_id=%s on binding for actor=%s goal=%s",
                story_id[:8] if story_id else "",
                str(assigned_actor_id)[:8], goal_id,
            )
            # Re-fetch to post the notice
            binding_row = await pool.fetchrow("""
                SELECT pcb.external_channel_id, pcb.integration_id
                  FROM platform_channel_bindings pcb
                 WHERE pcb.goal_id     = $1
                   AND pcb.actor_id    = $2
                   AND pcb.archived_at IS NULL
                 LIMIT 1
            """, goal_id, assigned_actor_id)
            if binding_row:
                creds = await get_integration_credentials(str(binding_row["integration_id"]), pool)
                bot_token = (creds or {}).get("bot_token", "")
                if bot_token:
                    try:
                        await _post_message(bot_token, binding_row["external_channel_id"], msg, username="System")
                    except Exception as exc:
                        _log.warning("Slack lifecycle: story in_progress notice failed: %s", exc)
        else:
            _log.warning(
                "Slack lifecycle: story=%s has no resolvable assignee - cannot route or provision channel",
                story_id[:8] if story_id else "",
            )
    else:
        _log.debug(
            "Slack lifecycle: story.state_changed - no active binding for story=%s goal=%s state=%s",
            story_id[:8] if story_id else "", goal_id, new_state,
        )


async def _on_story_created(goal_id: str, payload: dict) -> None:
    """Post a new-story announcement to the goal Slack channel."""
    from app.channels.slack.outbound import post_to_goal_page

    story_title = payload.get("story_title", "New story")
    assigned_to = payload.get("assigned_to", "")
    _log.info("Slack lifecycle: story.created goal=%s story=%r", goal_id, story_title)
    msg = f"📌 New story proposed: *{story_title}*"
    if assigned_to:
        msg += f" · assigned to {assigned_to}"
    await post_to_goal_page(goal_id, msg, username="Goal Orchestrator")


async def _on_goal_completed(goal_id: str, payload: dict) -> None:
    """Post a goal-completed notice to the Slack goal channel."""
    from app.channels.slack.outbound import post_to_goal_page

    goal_name = payload.get("goal_name", f"`{goal_id[:8]}`")
    _log.info("Slack lifecycle: goal.completed goal=%s name=%r", goal_id, goal_name)
    await post_to_goal_page(
        goal_id,
        f"✅ Goal *{goal_name}* completed.",
        username="System",
    )


async def _on_goal_deleted(goal_id: str, payload: dict) -> None:
    """Archive all Slack channels for this goal and post farewell notices.

    Must be called BEFORE platform_channel_bindings.archived_at is set so
    the relay can still look up active channel bindings.

    Slack channels can't be hard-deleted via bot API — archiving is the
    practical equivalent (read-only, drops out of the channel list).
    """
    from app.channels.slack.config import get_slack_config_for_goal, get_integration_credentials
    from app.channels.slack.outbound import _post_message
    from app.database import get_pool
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    goal_name = payload.get("goal_name", f"`{goal_id[:8]}`")
    _log.info(
        "Slack lifecycle: goal.deleted — archiving channels for goal=%s name=%r",
        goal_id, goal_name,
    )
    farewell = f"🗑 Goal *{goal_name}* has been deleted. This channel will no longer receive updates."

    pool = get_pool()
    cfg = await get_slack_config_for_goal(goal_id, pool)
    if not cfg or not cfg.get("bot_token"):
        _log.info("Slack lifecycle: goal.deleted — no Slack config for goal=%s, nothing to archive", goal_id)
        return

    bot_token = cfg["bot_token"]
    client = WebClient(token=bot_token)

    # Post farewell to goal channel, then archive
    if cfg.get("goal_channel_id"):
        try:
            await _post_message(bot_token, cfg["goal_channel_id"], farewell, username="System")
        except Exception as exc:
            _log.warning("Slack lifecycle: goal farewell failed channel=%s: %s", cfg["goal_channel_id"], exc)
        try:
            client.conversations_archive(channel=cfg["goal_channel_id"])
            _log.info("Slack lifecycle: archived goal channel=%s", cfg["goal_channel_id"])
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc))
            if err != "already_archived":
                _log.warning("Slack lifecycle: archive failed goal channel=%s: %s", cfg["goal_channel_id"], err)

    # Archive orch channel
    if cfg.get("orch_channel_id"):
        try:
            client.conversations_archive(channel=cfg["orch_channel_id"])
            _log.info("Slack lifecycle: archived orch channel=%s", cfg["orch_channel_id"])
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc))
            if err != "already_archived":
                _log.warning("Slack lifecycle: archive failed orch channel=%s: %s", cfg["orch_channel_id"], err)

    # Archive all user-goal channels (platform_channel_bindings with actor_id IS NOT NULL)
    integration_id = cfg["integration_id"]
    shca_bindings = await pool.fetch("""
        SELECT external_channel_id, actor_id
          FROM platform_channel_bindings
         WHERE goal_id        = $1
           AND integration_id = $2
           AND actor_id       IS NOT NULL
           AND archived_at    IS NULL
    """, goal_id, integration_id)

    for row in shca_bindings:
        chan = row["external_channel_id"]
        try:
            await _post_message(bot_token, chan, farewell, username="System")
        except Exception as exc:
            _log.warning("Slack lifecycle: user channel farewell failed channel=%s: %s", chan, exc)
        try:
            client.conversations_archive(channel=chan)
            _log.info("Slack lifecycle: archived user-goal channel=%s", chan)
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc))
            if err != "already_archived":
                _log.warning("Slack lifecycle: archive failed user channel=%s: %s", chan, err)

    _log.info(
        "Slack lifecycle: goal.deleted done — archived %d user-goal channels goal=%s",
        len(shca_bindings), goal_id,
    )


async def _on_task_created(goal_id: str, payload: dict) -> None:
    """Post a new-task notice to the goal Slack channel."""
    from app.channels.slack.outbound import post_to_goal_page

    task_title = payload.get("task_title", "New task")
    story_id = payload.get("story_id", "")
    _log.info("Slack lifecycle: task.created goal=%s task=%r story=%s", goal_id, task_title, story_id)
    msg = f"🔧 New task: *{task_title}*"
    if story_id:
        msg += f" · story `{story_id[:8]}`"
    await post_to_goal_page(goal_id, msg, username="System")


async def _on_task_state_changed(goal_id: str, payload: dict) -> None:
    """Post a task state-change notice to the user's channel, or goal channel as fallback."""
    from app.channels.slack.config import get_integration_credentials
    from app.channels.slack.outbound import _post_message, post_to_goal_page
    from app.database import get_pool

    task_id = payload.get("task_id", "")
    task_title = payload.get("task_title", "") or f"`{task_id[:8]}`"
    new_state = payload.get("new_state", "")
    state_label = _STATE_LABELS.get(new_state, new_state)
    msg = f"Task *{task_title}* → {state_label}"

    _log.info("Slack lifecycle: task.state_changed goal=%s task=%s → %s", goal_id, task_id[:8] if task_id else "", new_state)

    # Find the user-goal binding whose current_story_id owns this task
    pool = get_pool()
    story_id = payload.get("story_id") or ""
    if not story_id and task_id:
        story_id = await pool.fetchval("SELECT story_id FROM tasks WHERE id = $1", task_id) or ""

    binding_row = None
    if story_id:
        binding_row = await pool.fetchrow("""
            SELECT pcb.external_channel_id, pcb.integration_id
              FROM platform_channel_bindings pcb
              JOIN platform_integrations pi ON pi.id = pcb.integration_id
              JOIN goal_platform_integrations gpi ON gpi.integration_id = pcb.integration_id
                                                 AND gpi.goal_id = pcb.goal_id
             WHERE pcb.goal_id          = $1
               AND pcb.current_story_id = $2
               AND pcb.actor_id         IS NOT NULL
               AND pcb.archived_at      IS NULL
               AND pi.platform          = 'slack'
               AND pi.status            = 'active'
               AND gpi.enabled          = true
             LIMIT 1
        """, goal_id, story_id)

    if binding_row:
        creds = await get_integration_credentials(str(binding_row["integration_id"]), pool)
        bot_token = (creds or {}).get("bot_token", "")
        if bot_token:
            try:
                await _post_message(bot_token, binding_row["external_channel_id"], msg, username="System")
                _log.info(
                    "Slack lifecycle: task state notice sent goal=%s task=%s channel=%s",
                    goal_id, task_id[:8] if task_id else "", binding_row["external_channel_id"],
                )
                return
            except Exception as exc:
                _log.warning("Slack lifecycle: task state notice failed channel=%s: %s", binding_row["external_channel_id"], exc)

    # Fallback: goal channel
    await post_to_goal_page(goal_id, msg, username="System")


async def _on_member_joined(goal_id: str, payload: dict) -> None:
    """Post a member-joined notice to the goal Slack channel."""
    from app.channels.slack.outbound import post_to_goal_page

    display_name = payload.get("display_name") or payload.get("uid", "Someone")
    role = payload.get("role", GoalRole.GOAL_MEMBER)
    role_label = GOAL_ROLE_LABELS.get(role, role)
    _log.info("Slack lifecycle: member.joined goal=%s user=%s role=%s", goal_id, display_name, role_label)
    await post_to_goal_page(goal_id, f"👋 *{display_name}* joined as {role_label}", username="System")


async def _on_member_left(goal_id: str, payload: dict) -> None:
    """Post a member-left notice to the goal Slack channel."""
    from app.channels.slack.outbound import post_to_goal_page

    display_name = payload.get("display_name") or payload.get("uid", "Someone")
    _log.info("Slack lifecycle: member.left goal=%s user=%s", goal_id, display_name)
    await post_to_goal_page(goal_id, f"👋 *{display_name}* left the goal", username="System")


async def _on_resource_registered(goal_id: str, payload: dict) -> None:
    """Post a resource-registered notice to the goal Slack channel."""
    from app.channels.slack.outbound import post_to_goal_page

    resource_name = payload.get("resource_name") or payload.get("filename", "resource")
    kind = payload.get("kind") or payload.get("file_type", "")
    _log.info("Slack lifecycle: resource.registered goal=%s name=%r kind=%s", goal_id, resource_name, kind)
    msg = f"📦 Resource registered: *{resource_name}*"
    if kind:
        msg += f" ({kind})"
    await post_to_goal_page(goal_id, msg, username="System")


# ── Slack channel lifecycle helpers (used by provisioner) ─────────────────────

def _find_channel_by_name(client, name: str, is_private: bool = False) -> Optional[str]:
    """Find a Slack channel by name. Returns channel_id or None."""
    from slack_sdk.errors import SlackApiError
    types = "private_channel" if is_private else "public_channel"
    cursor = None
    for _ in range(10):
        resp = client.conversations_list(types=types, limit=200, cursor=cursor)
        for ch in resp.get("channels", []):
            if ch.get("name") == name:
                return ch.get("id")
        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
    return None


def create_or_join_public_channel(client, name: str) -> str:
    """Create a public channel; if name_taken, find and join it. Returns channel_id."""
    from slack_sdk.errors import SlackApiError
    try:
        result = client.conversations_create(name=name, is_private=False)
        channel_id = result["channel"]["id"]
        _log.info("Created public channel #%s (%s)", name, channel_id)
    except SlackApiError as exc:
        if exc.response.get("error") == "name_taken":
            _log.info("Channel #%s already exists, finding it", name)
            channel_id = _find_channel_by_name(client, name, is_private=False)
            if not channel_id:
                _log.error("Channel #%s reported name_taken but not found in list", name)
                raise
        else:
            raise

    try:
        client.conversations_join(channel=channel_id)
    except SlackApiError as exc:
        if exc.response.get("error") != "already_in_channel":
            _log.warning("Failed to join channel %s: %s", channel_id, exc)
    return channel_id


def create_private_channel(client, name: str) -> str:
    """Create a private channel, appending -2..-9 on name collision. Returns channel_id."""
    from slack_sdk.errors import SlackApiError
    for suffix in ("", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"):
        candidate = f"{name}{suffix}"[:80]
        try:
            result = client.conversations_create(name=candidate, is_private=True)
            channel_id = result["channel"]["id"]
            _log.info("Created private channel #%s (%s)", candidate, channel_id)
            return channel_id
        except SlackApiError as exc:
            if exc.response.get("error") == "name_taken":
                continue
            raise
    raise RuntimeError(f"Could not create private channel after 9 attempts from base {name}")


async def setup_goal_channels(
    goal_id: str,
    goal_name: str,
    integration_id: str,
    bot_token: str,
    pool,
    channel_name: str = "",
    existing_channel_id: str = "",
) -> dict:
    """Create the goal + orchestrator Slack channels and invite all goal members.

    Returns: {goal_channel_id, goal_channel_name, orch_channel_id, orch_channel_name}
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    from app.channels.slack.thread_map import map_slack_user

    client = WebClient(token=bot_token)
    goal_slug = goal_id.lower() + _channel_env_suffix()

    # 1. Goal channel
    if existing_channel_id:
        goal_channel_id = existing_channel_id
        try:
            client.conversations_join(channel=goal_channel_id)
        except SlackApiError as exc:
            if exc.response.get("error") != "already_in_channel":
                _log.warning("Failed to join existing channel %s: %s", goal_channel_id, exc)
    else:
        goal_channel_id = create_or_join_public_channel(client, goal_slug)

    try:
        info = client.conversations_info(channel=goal_channel_id)
        goal_channel_name = info["channel"]["name"]
    except Exception:
        goal_channel_name = goal_slug

    # 2. Orchestrator channel — DISABLED for goals provisioned from now on.
    # Agent Mesh / Goal Review Council / Security Council are staying
    # in-app-only for new goals (see council_thread.py, security_thread.py,
    # orchestrator.py); goals that already have an orch_channel_id keep
    # working unchanged (post_to_orchestrator_channel no-ops when it's
    # missing). Commented out, not deleted — intended to come back later.
    # orch_slug = _slugify(f"{goal_channel_name}-orchestrator")
    # orch_channel_id = create_or_join_public_channel(client, orch_slug)
    # try:
    #     info = client.conversations_info(channel=orch_channel_id)
    #     orch_channel_name = info["channel"]["name"]
    # except Exception:
    #     orch_channel_name = orch_slug
    orch_channel_id = ""
    orch_channel_name = ""

    # 3. Welcome messages
    try:
        client.chat_postMessage(
            channel=goal_channel_id,
            text=(
                f"*Goal:* {goal_name}\n"
                f"Chat here to talk to the Goal Orchestrator.\n"
                f"The assistant will help you work on stories assigned to you."
            ),
            username="Goal Orchestrator",
        )
    except SlackApiError as exc:
        _log.warning("Failed to post goal welcome: %s", exc)

    # Orchestrator channel welcome — disabled alongside its provisioning above.
    # if orch_channel_id:
    #     try:
    #         client.chat_postMessage(
    #             channel=orch_channel_id,
    #             text=(
    #                 ":information_source: *Orchestrator channel (read-only).*\n"
    #                 "The Goal Orchestrator and assistants coordinate here. "
    #                 "Human messages are deleted — use the goal channel to talk to the GO."
    #             ),
    #             username="System",
    #         )
    #     except SlackApiError as exc:
    #         _log.warning("Failed to post orch notice: %s", exc)

    # 4. Invite all goal members with a Slack account
    members = await pool.fetch(
        """SELECT u.uid, u.email, u.display_name
             FROM goal_members gm
             JOIN users u ON u.uid = gm.uid
            WHERE gm.goal_id = $1 AND u.email <> ''""",
        goal_id,
    )
    slack_user_ids: list[str] = []
    workspace_id = ""
    for m in members:
        try:
            resp = client.users_lookupByEmail(email=m["email"])
            slack_id = (resp.get("user") or {}).get("id", "")
            profile = (resp.get("user") or {}).get("profile") or {}
        except SlackApiError as exc:
            err = (exc.response or {}).get("error", str(exc))
            if err != "users_not_found":
                _log.warning("users.lookupByEmail failed for %s: %s", m["email"], err)
            continue
        except Exception as exc:
            _log.warning("users.lookupByEmail error for %s: %s", m["email"], exc)
            continue

        if not slack_id:
            continue
        slack_user_ids.append(slack_id)
        try:
            await map_slack_user(
                slack_user_id=slack_id,
                user_uid=m["uid"],
                slack_display_name=(profile.get("display_name") or profile.get("real_name") or ""),
                slack_email=m["email"],
                pool=pool,
            )
        except Exception as exc:
            _log.debug("map_slack_user failed for %s: %s", slack_id, exc)

    if slack_user_ids:
        users_csv = ",".join(slack_user_ids)
        # orch channel invite skipped — no orch_channel_id while provisioning is disabled above.
        invite_targets = [(goal_channel_id, "goal")]
        if orch_channel_id:
            invite_targets.append((orch_channel_id, "orch"))
        for channel_id, label in invite_targets:
            try:
                client.conversations_invite(channel=channel_id, users=users_csv)
                _log.info("Invited %d members to %s channel %s", len(slack_user_ids), label, channel_id)
            except SlackApiError as exc:
                err = (exc.response or {}).get("error", str(exc))
                if err not in ("already_in_channel", "cant_invite_self"):
                    _log.warning("conversations.invite failed for %s channel: %s", label, err)

    _log.info(
        "Slack goal channels ready: goal=%s #%s (%s) + #%s (%s) — invited %d members",
        goal_id, goal_channel_name, goal_channel_id,
        orch_channel_name, orch_channel_id, len(slack_user_ids),
    )
    return {
        "goal_channel_id": goal_channel_id,
        "goal_channel_name": goal_channel_name,
        "orch_channel_id": orch_channel_id,
        "orch_channel_name": orch_channel_name,
    }


async def ensure_user_goal_channel(
    goal_id: str,
    user_uid: str,
    user_actor_id,
    user_display_name: str,
    slack_user_id: str,
    thread_id: str,
    agent_id: str,
    integration_id: str,
    bot_token: str,
    pool,
    story: Optional[dict] = None,
) -> Optional[str]:
    """Find or create the private (user × goal) Slack channel.

    Stores the binding in platform_channel_bindings.
    Returns the Slack channel_id, or None on failure.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=bot_token)

    # Check for existing binding
    existing = await pool.fetchrow(
        """SELECT external_channel_id FROM platform_channel_bindings
           WHERE goal_id = $1 AND integration_id = $2 AND actor_id = $3 AND archived_at IS NULL
           LIMIT 1""",
        goal_id, integration_id, user_actor_id,
    )

    if existing:
        channel_id = existing["external_channel_id"]

        # Rejoin case: this channel may have been shared read-only with the
        # rest of the goal while this member was removed (see
        # SlackProvisioner.deprovision_member / _archive_and_share_personal_channel
        # in app/channels/slack/provisioner.py). Unarchive it and kick out
        # everyone but the returning member before continuing — the same
        # channel is reused, not recreated, so history/context survive intact.
        try:
            info = client.conversations_info(channel=channel_id)
            if info.get("channel", {}).get("is_archived"):
                client.conversations_unarchive(channel=channel_id)
                _log.info(
                    "ensure_user_goal_channel: unarchived channel=%s for returning actor=%s goal=%s",
                    channel_id, str(user_actor_id)[:8], goal_id,
                )
                try:
                    members_resp = client.conversations_members(channel=channel_id)
                    for member_id in members_resp.get("members", []):
                        if member_id == slack_user_id:
                            continue
                        try:
                            client.conversations_kick(channel=channel_id, user=member_id)
                        except SlackApiError as exc:
                            err = (exc.response or {}).get("error", "")
                            if err not in ("cant_kick_self", "cant_kick_from_general"):
                                _log.warning(
                                    "ensure_user_goal_channel: kick failed member=%s channel=%s: %s",
                                    member_id, channel_id, exc,
                                )
                except SlackApiError as exc:
                    _log.warning(
                        "ensure_user_goal_channel: could not list members to clear channel=%s: %s",
                        channel_id, exc,
                    )
        except SlackApiError as exc:
            _log.warning(
                "ensure_user_goal_channel: archive-state check failed channel=%s: %s", channel_id, exc,
            )

        # Re-invite (silent if already member)
        if slack_user_id:
            try:
                client.conversations_invite(channel=channel_id, users=slack_user_id)
            except SlackApiError as exc:
                err = (exc.response or {}).get("error", "")
                if err not in ("already_in_channel", "user_is_bot", "cant_invite_self"):
                    _log.warning("ensure_user_goal_channel re-invite failed %s/%s: %s", slack_user_id, channel_id, exc)

        if story:
            # Rotate current_story_id
            await pool.execute(
                """UPDATE platform_channel_bindings
                      SET current_story_id = $1, thread_id = $2, agent_id = $3
                    WHERE goal_id = $4 AND integration_id = $5 AND actor_id = $6 AND archived_at IS NULL""",
                story["id"], thread_id, agent_id, goal_id, integration_id, user_actor_id,
            )
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    text=(
                        f":arrows_counterclockwise: *Now working on:* {story['title']}\n"
                        f"_The assistant is reading RESOURCES.md and will start planning shortly._"
                    ),
                    username="System",
                )
            except SlackApiError as exc:
                _log.warning("Failed to post story-rotate notice: %s", exc)
        else:
            # Update thread_id / agent_id in case SHCA was reaped and recreated
            await pool.execute(
                """UPDATE platform_channel_bindings
                      SET thread_id = $1, agent_id = $2
                    WHERE goal_id = $3 AND integration_id = $4 AND actor_id = $5 AND archived_at IS NULL""",
                thread_id, agent_id, goal_id, integration_id, user_actor_id,
            )
        _log.info(
            "ensure_user_goal_channel: reused channel=%s for actor=%s goal=%s",
            channel_id, str(user_actor_id)[:8], goal_id,
        )
        return channel_id

    # First time: build channel name and create it
    channel_name = _channel_name_for(goal_id, user_display_name)

    try:
        channel_id = create_private_channel(client, channel_name)
    except Exception as exc:
        _log.error("ensure_user_goal_channel: failed to create channel %s: %s", channel_name, exc)
        return None

    try:
        info = client.conversations_info(channel=channel_id)
        final_name = info["channel"]["name"]
    except Exception:
        final_name = channel_name

    if slack_user_id:
        try:
            client.conversations_invite(channel=channel_id, users=slack_user_id)
        except SlackApiError as exc:
            err = (exc.response or {}).get("error", "")
            if err not in ("already_in_channel", "user_is_bot"):
                _log.warning("Failed to invite user %s to new channel %s: %s", slack_user_id, channel_id, exc)

    # Persist binding in platform_channel_bindings
    await pool.execute(
        """INSERT INTO platform_channel_bindings
               (goal_id, integration_id, thread_id, actor_id, agent_id,
                external_channel_id, current_story_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT (goal_id, integration_id, external_channel_id)
           DO UPDATE SET thread_id = $3, agent_id = $5,
                         current_story_id = $7, archived_at = NULL""",
        goal_id, integration_id, thread_id, user_actor_id, agent_id,
        channel_id, story["id"] if story else None,
    )

    # Welcome message
    try:
        if story:
            desc = (story.get("description") or "")[:500]
            parts = [
                "*Welcome.* This is your assistant channel for this goal.",
                f"*Now working on:* {story['title']}",
            ]
            if desc:
                parts.append(f"\n{desc}")
            parts.append(
                "\n_The assistant has loaded the story context and will start planning shortly. "
                "Stories rotate through this channel — your history persists for the lifetime of the goal._"
            )
            text = "\n".join(parts)
        else:
            text = (
                "*Welcome.* This is your assistant channel for this goal. "
                "Ask the Goal Orchestrator to assign you a story to start working on it."
            )
        client.chat_postMessage(channel=channel_id, text=text, username="System")
    except SlackApiError as exc:
        _log.warning("Failed to post user-goal-channel welcome: %s", exc)

    _log.info(
        "ensure_user_goal_channel: created channel=%s (#%s) for actor=%s goal=%s",
        channel_id, final_name, str(user_actor_id)[:8], goal_id,
    )
    return channel_id