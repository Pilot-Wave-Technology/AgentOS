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

from app.channels.slack.client import make_async_client
import logging
import re
from typing import Optional

from app.roles import GoalRole, GOAL_ROLE_LABELS

_log = logging.getLogger(__name__)


def _channel_env_suffix() -> str:
    """The environment's channel-name suffix, e.g. "-dev".

    Empty for the PRODUCTION environment, which takes the clean names --
    `#goal1`, not `#goal1-next`. Every other environment must set
    SLACK_COMMAND_SUFFIX, and `require_env_separation()` below refuses to
    provision if it has not, because all environments share one Slack
    workspace and the suffix is the only thing keeping their channels
    apart.
    """
    from app.config import get_settings
    suffix = (get_settings().slack_command_suffix or "").strip().lower()
    return f"-{suffix}" if suffix else ""


def require_env_separation() -> None:
    """Refuse to create channels from a non-production env with no suffix.

    An unsuffixed name from dev lands in the same namespace as production's,
    and `create_private_channel` will now raise on the collision --
    but only AFTER asking Slack, and only for a goal whose name happens to
    clash. Checking up front turns "a confusing failure, eventually, for
    one goal" into "this environment is misconfigured, now, for all of
    them".
    """
    from app.config import get_settings
    st = get_settings()
    if (st.slack_command_suffix or "").strip():
        return
    if (getattr(st, "app_env", "") or "").lower() == "production":
        return          # production legitimately runs unsuffixed
    raise RuntimeError(
        "SLACK_COMMAND_SUFFIX is not set and this is not the production "
        "environment. All environments share one Slack workspace, so an "
        "unsuffixed channel name here would collide with production's."
    )


# `_slugify_for_slack`, `_channel_name_for` and `ensure_user_goal_channel`
# WERE HERE AND ARE GONE (2026-10-19).
#
# They maintained a private Slack channel per (member x goal) -- a tier
# that predated story channels and had stopped carrying anything: the
# conversation, the cards and the agent's replies all land in the story's
# own channel now.
#
# It was also actively wrong. The lookup matched only an UNARCHIVED
# binding for the actor, so once a binding was archived it created a
# FRESH Slack channel while the old one still existed -- Slack appended a
# suffix, and `goal2-<name>-feature-2` appeared. The unarchive handling it
# carried sat on the found-a-binding branch, unreachable from the single
# state that needed it. Left alone it would have produced -3, -4.
#
# `thread_channels.ensure_channel_for_thread` replaces it and does not
# have that failure mode: a unique index on (integration_id, thread_id)
# makes a second caller adopt the first's channel instead of creating
# another, and it records `channel_name` and invites participants --
# neither of which this path did, which is why those bindings are the
# only ones in the table with a NULL name.
# EVERY state a story can be in, not most of them. `awaiting_merge` and
# `reviewed` were missing, so a story entering either announced itself in
# Slack with the raw column value -- "Story status update: Build the thing
# → awaiting_merge" -- which reads like a bug to anyone who does not know
# the schema. The fallback hides the omission instead of surfacing it,
# which is why nobody noticed; a test now walks the states the code
# actually transitions to and fails on one with no label.
_STATE_LABELS = {
    "proposed": "📋 Proposed",
    "accepted": "✅ Accepted",
    "in_progress": "🔄 In Progress",
    "blocked": "⚠️ Blocked",
    "awaiting_merge": "⏳ Waiting to merge",
    "done": "✅ Done",
    "reviewed": "🔍 Reviewed",
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
                # NOTHING TO PROVISION ANY MORE. This used to call
                # `_do_provision_member` to create the member's private
                # (member x goal) channel, and that tier is gone -- it is
                # also exactly where `<goal>-<name>-<env>-2` was born,
                # because an archived binding sent it to create a second
                # Slack channel beside the live one.
                #
                # `UPDATE 0` now means only that the member has no
                # user-goal binding, which after the removal is the
                # ordinary case rather than a gap to fill. The story's
                # own channel carries the story; `current_story_id` on a
                # tier that no longer exists has nothing to point at.
                _log.debug(
                    "Slack lifecycle: story %s -> %s, no user-goal binding for "
                    "actor=%s goal=%s (expected; story channels carry this)",
                    story_id[:8], new_state, str(assigned_actor_id)[:8], goal_id,
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
    client = make_async_client(bot_token)

    # Post farewell to goal channel, then archive
    if cfg.get("goal_channel_id"):
        try:
            await _post_message(bot_token, cfg["goal_channel_id"], farewell, username="System")
        except Exception as exc:
            _log.warning("Slack lifecycle: goal farewell failed channel=%s: %s", cfg["goal_channel_id"], exc)
        try:
            await client.conversations_archive(channel=cfg["goal_channel_id"])
            _log.info("Slack lifecycle: archived goal channel=%s", cfg["goal_channel_id"])
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc))
            if err != "already_archived":
                _log.warning("Slack lifecycle: archive failed goal channel=%s: %s", cfg["goal_channel_id"], err)

    # Archive orch channel
    if cfg.get("orch_channel_id"):
        try:
            await client.conversations_archive(channel=cfg["orch_channel_id"])
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
            await client.conversations_archive(channel=chan)
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

class ChannelNameTaken(RuntimeError):
    """A name we expected to own is already in use by somebody else."""


async def _adopt_own_orphan(client, name: str) -> str:
    """The id of a channel of this name that OUR bot created, or "".

    Only ever called on `name_taken`. Returns "" for anything we did not
    create, for an archived channel (joining one is not possible and the
    name is gone for good), and for any lookup failure -- the caller
    then raises, which is the safe direction.
    """
    from slack_sdk.errors import SlackApiError
    try:
        me = (await client.auth_test()).get("user_id", "")
        if not me:
            return ""
        cursor = None
        for _ in range(10):                      # 10 x 200 channels
            resp = await client.conversations_list(
                types="private_channel,public_channel", limit=200,
                exclude_archived=True, cursor=cursor)
            for ch in resp.get("channels", []):
                if ch.get("name") != name:
                    continue
                if ch.get("creator") != me:
                    return ""                    # somebody else's
                _log.warning(
                    "Adopting #%s (%s) — this bot created it and nothing is "
                    "bound to it, so it is an orphan from a failed run.",
                    name, ch.get("id"))
                # NO JOIN FOR A PRIVATE CHANNEL. `conversations.join` is
                # public-channels-only and answers
                # `method_not_supported_for_channel_type` -- and the bot
                # is already a member of one it created, so there is
                # nothing to join. The first version of this treated any
                # error but `already_in_channel` as fatal and so threw
                # away an adoption it had already got right.
                if not ch.get("is_private"):
                    try:
                        await client.conversations_join(channel=ch["id"])
                    except SlackApiError as exc:
                        if exc.response.get("error") not in (
                                "already_in_channel",
                                "method_not_supported_for_channel_type"):
                            raise
                return ch.get("id") or ""
            cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Could not check whether #%s is our own orphan: %s",
                     name, exc)
    return ""


async def create_private_channel(client, name: str, *,
                                 allow_suffix: bool = False) -> str:
    """Create a private channel. Returns channel_id.

    `allow_suffix=False` (the default) means a taken name RAISES, for
    the same reason the goal channel does: a name we
    expect to own being taken is a misconfiguration -- usually a missing
    environment prefix -- and quietly taking a different name hides it.

    `allow_suffix=True` is for the one family of names that can
    legitimately collide: the legacy per-member channels, derived from a
    display name, where two people really can be called the same thing.
    Names built by `naming.channel_name` carry a thread id and are unique
    by construction, so they never need it.
    """
    from slack_sdk.errors import SlackApiError
    if not allow_suffix:
        try:
            result = await client.conversations_create(name=name[:80],
                                                       is_private=True)
            _log.info("Created private channel #%s (%s)", name,
                      result["channel"]["id"])
            return result["channel"]["id"]
        except SlackApiError as exc:
            if exc.response.get("error") == "name_taken":
                # OUR OWN ORPHAN IS THE ONE EXCEPTION, and it is the
                # common case rather than a rare one: a provisioning run
                # that creates the channel and then fails before writing
                # the binding leaves exactly this -- a channel with our
                # bot as its creator and nothing pointing at it. Seen on
                # feature within an hour of the guard shipping.
                #
                # Refusing it permanently would be worse than joining a
                # stranger's channel, because there is no way back:
                # archiving does not release the name, so the only
                # recovery is a human renaming the channel by hand, and
                # nothing tells them that.
                #
                # Adopting is still narrow. The channel must have been
                # created BY THIS BOT -- not merely be reachable -- which
                # is the distinction the old join-on-name_taken behaviour
                # failed to draw when it bound one environment's goal to
                # another's channel.
                adopted = await _adopt_own_orphan(client, name)
                if adopted:
                    return adopted
                _log.error(
                    "Private channel #%s already exists and was not created "
                    "by this bot — refusing to take it over. Check "
                    "SLACK_COMMAND_SUFFIX.", name)
                raise ChannelNameTaken(
                    f"#{name} already exists in this workspace and was not "
                    f"created by this deployment. Rename it in Slack "
                    f"(archiving does NOT free the name) and try again."
                ) from exc
            raise

    for suffix in ("", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"):
        candidate = f"{name}{suffix}"[:80]
        try:
            result = await client.conversations_create(name=candidate, is_private=True)
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
    from slack_sdk.errors import SlackApiError
    from app.channels.slack.thread_map import map_slack_user

    require_env_separation()
    client = make_async_client(bot_token)
    goal_slug = goal_id.lower() + _channel_env_suffix()

    # 1. Goal channel
    if existing_channel_id:
        goal_channel_id = existing_channel_id
        try:
            await client.conversations_join(channel=goal_channel_id)
        except SlackApiError as exc:
            if exc.response.get("error") != "already_in_channel":
                _log.warning("Failed to join existing channel %s: %s", goal_channel_id, exc)
    else:
        # PRIVATE. The goal channel used to be public, so anyone in the
        # Slack workspace could find and read a goal's conversation --
        # including people with no role on it. Slack has no public-API
        # way to convert a public channel to private
        # (admin.conversations.convertToPrivate is Enterprise Grid only),
        # so goals provisioned before this keep their public channel until
        # somebody archives it and re-enables Slack for the goal. New ones
        # are private from the start.
        goal_channel_id = await create_private_channel(client, goal_slug)

    try:
        info = await client.conversations_info(channel=goal_channel_id)
        goal_channel_name = info["channel"]["name"]
    except Exception:
        goal_channel_name = goal_slug

    # The orchestrator channel is GONE, not disabled. Its provisioning
    # had been commented out for some time while post_to_orchestrator_channel
    # and find_goal_by_orch_channel stayed live -- a half-path nobody owned,
    # which read as a feature in the code and did nothing in practice. Under
    # one-channel-per-thread an orchestrator thread is simply a thread and
    # gets a channel the same way every other thread does.
    orch_channel_id = ""
    orch_channel_name = ""

    # 3. Welcome messages
    try:
        await client.chat_postMessage(
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
    #         await client.chat_postMessage(
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
            resp = await client.users_lookupByEmail(email=m["email"])
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
                await client.conversations_invite(channel=channel_id, users=users_csv)
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
