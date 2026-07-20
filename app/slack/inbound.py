"""DEPRECATED — superseded by app/channels/slack/inbound.py (SlackInboundAdapter).
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Inbound Slack → control panel routing (channel-per-story model).

Routing by channel:
  - Message in goal channel            → route to GO session (plain message)
  - Message in goal channel w/ mention → "work on story" flow (create story channel)
  - Message in orchestrator channel    → delete (read-only for humans)
  - Message in mapped story channel    → route to SHCA session (only assigned user)
  - Message elsewhere                  → ignore
"""
import asyncio
import json
import logging
import re
import uuid

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.database import get_pool
from app.slack.config import (
    get_goal_by_channel,
    get_goal_by_orch_channel,
    get_goal_slack_config,
    get_workspace,
)
from app.slack.thread_map import (
    get_user_uid,
    auto_map_user_by_email,
)
from app.slack import story_channels as story_ch
from app.slack import user_goal_channels as ugc
from app.slack.lifecycle import (
    create_story_channel as lifecycle_create_story_channel,
    ensure_user_goal_channel,
)

_log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


async def _resolve_slack_mentions_in_text(text: str, team_id: str, bot_token: str) -> str:
    """Replace <@SLACK_USER_ID> with @display_name (uid: <control-panel-uid>)
    so an agent reading the message can resolve the reference to a team
    roster entry.

    Falls back to @SLACK_DISPLAY_NAME (no uid) if the user isn't mapped to
    our DB yet — still useful to the agent.
    """
    if not text or "<@" not in text:
        return text

    mentions = set(_MENTION_RE.findall(text))
    if not mentions:
        return text

    pool = get_pool()
    replacements: dict[str, str] = {}
    for slack_uid in mentions:
        # 1) Try our mapping first — fastest path, gives uid.
        uid = await get_user_uid(slack_uid, team_id)
        display = ""
        if uid:
            display = await pool.fetchval(
                "SELECT display_name FROM users WHERE uid = $1", uid,
            ) or ""
        if not display:
            # 2) Fall back to Slack profile for the display name only.
            try:
                from slack_sdk import WebClient
                client = WebClient(token=bot_token)
                info = client.users_info(user=slack_uid)
                profile = (info.get("user") or {}).get("profile", {}) or {}
                display = profile.get("display_name") or profile.get("real_name") or ""
            except Exception as exc:
                _log.debug("Slack mention lookup failed for %s: %s", slack_uid, exc)
        if display:
            if uid:
                replacements[slack_uid] = f"@{display} (uid: {uid})"
            else:
                replacements[slack_uid] = f"@{display}"
        # else: leave the raw <@...> in place; agent might still handle it

    if not replacements:
        return text

    def _sub(m):
        slack_uid = m.group(1)
        return replacements.get(slack_uid, m.group(0))

    return _MENTION_RE.sub(_sub, text)


async def handle_slack_event(team_id: str, event: dict) -> None:
    """Route a Slack event to the appropriate handler."""
    event_type = event.get("type", "")
    channel = event.get("channel", "")
    user = event.get("user", "")
    text = event.get("text", "")

    # member_joined_channel: a Slack user was added to one of our channels.
    # We gate membership by email — if the new member's email matches a
    # row in `users`, they're added to the goal as a member; otherwise
    # they're kicked and DM'd to explain why.
    if event_type == "member_joined_channel":
        await _handle_member_joined_channel(team_id, event)
        return

    if not channel or not user:
        return

    # Drop synthetic message events that Slack auto-generates for
    # channel membership changes (joined / left / topic changes / bot
    # adds / etc.). These look like real `message` events — same shape,
    # `user` populated, `text` contains the auto-rendered "X joined" —
    # but they're not human input and routing them to the SHCA causes
    # the kickoff to race against an unwanted "joined the channel"
    # auto-prompt, which the SHCA then treats as a turn and replies to
    # before our actual kickoff lands.
    subtype = event.get("subtype", "") or ""
    if subtype in (
        "channel_join", "channel_leave",
        "channel_topic", "channel_purpose", "channel_name",
        "bot_add", "bot_remove",
        "group_join", "group_leave",
        "channel_archive", "channel_unarchive",
    ):
        _log.debug(
            "Skipping synthetic Slack message subtype=%s in channel=%s",
            subtype, channel,
        )
        return

    # Dedup: Slack delivers both `app_mention` and `message.channels` when a user
    # @mentions the bot. Treat `app_mention` as canonical — skip the duplicate
    # message event if it contains the bot mention.
    if event_type == "message":
        ws = await get_workspace(team_id)
        bot_user_id = (ws or {}).get("bot_user_id", "")
        if bot_user_id and f"<@{bot_user_id}>" in text:
            _log.debug("Skipping message event that duplicates app_mention")
            return

    _log.info(
        "Slack inbound: type=%s team=%s channel=%s user=%s",
        event_type, team_id, channel, user,
    )

    # 1. Goal channel?
    config = await get_goal_by_channel(team_id, channel)
    if config:
        await _handle_goal_channel_event(team_id, config, event)
        return

    # 2. Orchestrator channel?
    orch = await get_goal_by_orch_channel(team_id, channel)
    if orch:
        await _handle_orch_channel_event(orch, event)
        return

    # 3. User-goal channel? (post-pivot: one channel per user × goal)
    ugc_mapping = await ugc.get_by_channel_id(channel)
    if ugc_mapping and not ugc_mapping.get("archived_at"):
        await _handle_user_goal_channel_event(team_id, ugc_mapping, event)
        return

    # 4. Legacy per-story channel? (kept for back-compat with existing data)
    story_mapping = await story_ch.get_by_channel_id(channel)
    if story_mapping and not story_mapping.get("archived_at"):
        await _handle_story_channel_event(team_id, story_mapping, event)
        return

    _log.debug("Slack channel %s not mapped to any goal — ignoring", channel)


# ── Goal channel ─────────────────────────────────────────────────────────────

async def _handle_goal_channel_event(team_id: str, config: dict, event: dict) -> None:
    """Message in the goal channel. Either @mention → work-on-story flow, or plain → GO."""
    goal_id = config["goal_id"]
    bot_token = config["bot_token"]
    bot_user_id = config.get("bot_user_id", "") or ""
    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    text = event.get("text", "") or ""
    event_type = event.get("type", "")
    files = event.get("files") or []

    user_uid = await _resolve_user(team_id, slack_user, bot_token, channel)
    if not user_uid:
        return

    # File uploads → register as goal_resources (no story link in goal channel)
    if files:
        await _handle_file_uploads(
            goal_id=goal_id, channel=channel, slack_user=slack_user,
            user_uid=user_uid, files=files, bot_token=bot_token,
            story_id="",
        )
        # If the message also has meaningful text beyond the file-share stub,
        # continue with normal routing; otherwise stop here.
        if not text.strip():
            return

    # Is this a bot mention (work-on-story trigger)?
    is_mention = event_type == "app_mention" or (
        bot_user_id and f"<@{bot_user_id}>" in text
    )
    if is_mention:
        await _handle_work_on_story(
            team_id=team_id,
            config=config,
            slack_user=slack_user,
            user_uid=user_uid,
            text=text,
            bot_token=bot_token,
        )
        return

    # Resolve <@SLACK_ID> mentions → @display_name (uid: …) so the GO can
    # look the mentioned person up in its team roster.
    resolved_text = await _resolve_slack_mentions_in_text(text, team_id, bot_token)

    # Plain message → GO
    await _route_to_go(goal_id, user_uid, resolved_text, channel=channel, bot_token=bot_token)


# ── Orchestrator channel ─────────────────────────────────────────────────────

async def _handle_orch_channel_event(config: dict, event: dict) -> None:
    """Read-only: delete human messages, send ephemeral warning."""
    # event.bot_id/bot_user_id are already filtered upstream in router.py
    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    ts = event.get("ts", "")
    bot_token = config["bot_token"]
    goal_channel_id = config.get("goal_channel_id", "")

    client = WebClient(token=bot_token)
    try:
        goal_ref = f"<#{goal_channel_id}>" if goal_channel_id else "the goal channel"
        client.chat_postEphemeral(
            channel=channel, user=slack_user,
            text=(
                f"This is a read-only coordination log between the Goal Orchestrator "
                f"and assistants. Use {goal_ref} to talk to the GO."
            ),
        )
        client.chat_delete(channel=channel, ts=ts)
        _log.info("Deleted human message in orch channel: channel=%s ts=%s", channel, ts)
    except SlackApiError as exc:
        _log.warning("Orch read-only enforcement failed: %s", exc)


# ── User-goal channel (post-pivot routing) ──────────────────────────────────

async def _handle_user_goal_channel_event(team_id: str, mapping: dict, event: dict) -> None:
    """Message in a private (user × goal) channel. The persistent SHCA
    conversation handles whatever story is currently active (or none)."""
    goal_id = mapping["goal_id"]
    thread_id = mapping["thread_id"]
    agent_id = mapping["agent_id"]
    mapped_uid = mapping["user_uid"]
    current_story_id = mapping.get("current_story_id") or ""
    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    text = event.get("text", "") or ""
    files = event.get("files") or []

    config = await get_goal_slack_config(goal_id)
    if not config:
        _log.warning("No slack_config for goal %s (user-goal channel %s)", goal_id, channel)
        return
    bot_token = config["bot_token"]

    user_uid = await _resolve_user(team_id, slack_user, bot_token, channel)
    if not user_uid:
        return

    if user_uid != mapped_uid:
        # Another goal admin posted in someone else's user-goal channel
        # (they may have been added). Allow but log.
        _log.info(
            "Non-owner %s posted in user-goal channel %s (owner=%s) — allowing",
            user_uid, channel, mapped_uid,
        )

    # File uploads → register as goal_resources (linked to current story
    # if one is active; otherwise goal-level).
    if files:
        await _handle_file_uploads(
            goal_id=goal_id, channel=channel, slack_user=slack_user,
            user_uid=user_uid, files=files, bot_token=bot_token,
            story_id=current_story_id,
        )
        if not text.strip():
            return

    resolved_text = await _resolve_slack_mentions_in_text(text, team_id, bot_token)
    await _route_to_shca(goal_id, thread_id, agent_id, user_uid, resolved_text, channel=channel, bot_token=bot_token)


# ── Story channel (legacy — kept for back-compat with existing data) ────────

async def _handle_story_channel_event(team_id: str, mapping: dict, event: dict) -> None:
    """Message in a private story channel. Only the mapped user may post."""
    goal_id = mapping["goal_id"]
    thread_id = mapping["thread_id"]
    agent_id = mapping["agent_id"]
    mapped_uid = mapping["user_uid"]
    story_id = mapping.get("story_id", "")
    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    text = event.get("text", "") or ""
    files = event.get("files") or []

    config = await get_goal_slack_config(goal_id)
    if not config:
        _log.warning("No slack_config for goal %s (story channel %s)", goal_id, channel)
        return
    bot_token = config["bot_token"]

    user_uid = await _resolve_user(team_id, slack_user, bot_token, channel)
    if not user_uid:
        return

    if user_uid != mapped_uid:
        # Someone other than the assigned user posted in their story channel
        # (e.g. another goal admin who added themselves). Allow for now but log.
        _log.info(
            "Non-owner %s posted in story channel %s (owner=%s) — allowing",
            user_uid, channel, mapped_uid,
        )

    # File uploads → register as goal_resources, linked to this story
    if files:
        await _handle_file_uploads(
            goal_id=goal_id, channel=channel, slack_user=slack_user,
            user_uid=user_uid, files=files, bot_token=bot_token,
            story_id=story_id,
        )
        if not text.strip():
            return

    # Resolve <@SLACK_ID> mentions so the SHCA sees @display_name (uid: …)
    resolved_text = await _resolve_slack_mentions_in_text(text, team_id, bot_token)
    await _route_to_shca(goal_id, thread_id, agent_id, user_uid, resolved_text, channel=channel, bot_token=bot_token)


# ── Work-on-story flow ───────────────────────────────────────────────────────

async def _handle_work_on_story(
    team_id: str,
    config: dict,
    slack_user: str,
    user_uid: str,
    text: str,
    bot_token: str,
) -> None:
    goal_id = config["goal_id"]
    goal_channel_id = config["goal_channel_id"]
    goal_slug = config.get("goal_channel_name", "") or "goal"
    client = WebClient(token=bot_token)
    pool = get_pool()

    clean = _MENTION_RE.sub("", text).strip()
    clean = re.sub(r"^work on\s+", "", clean, flags=re.IGNORECASE).strip()

    # Fetch candidate stories for this goal
    stories = await pool.fetch("""
        SELECT id, title, state, assigned_to, description, priority, depends_on
          FROM stories
         WHERE goal_id = $1 AND state NOT IN ('done', 'cancelled', 'rejected')
           -- Merge stories are landed by the Git Engine, not /work-able by a human.
           AND COALESCE(story_kind, 'code') <> 'merge'
         ORDER BY created_at DESC
    """, goal_id)

    story = _match_story(stories, clean)
    if not story:
        _ephemeral_story_list(client, goal_channel_id, slack_user, stories, user_uid)
        return

    # Assignment check
    if story["assigned_to"] and story["assigned_to"] != user_uid:
        client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text=(
                f"*{story['title']}* is assigned to someone else. "
                f"Ask the Goal Orchestrator to reassign if you want to take it."
            ),
        )
        return
    if not story["assigned_to"]:
        # Claim it
        await pool.execute(
            "UPDATE stories SET assigned_to = $1, updated_at = now() WHERE id = $2",
            user_uid, story["id"],
        )

    # Dependency check
    blockers = await _unmet_dependencies(story)
    if blockers:
        blocker_list = "\n".join(f"• *{b['title']}* ({b['state']})" for b in blockers)
        client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text=f"*{story['title']}* is blocked by:\n{blocker_list}",
        )
        return

    # Resource readiness check. RESOURCES.md is generated from
    # story_resources joined to goal_resources — if the story has zero
    # active grants, the SHCA's RESOURCES.md is essentially empty
    # (~60 bytes of boilerplate header) and it can't propose a
    # meaningful task spec. Bail early with a clear pointer at the GO
    # rather than letting the kickoff fire against an empty workspace.
    res_count = await pool.fetchval("""
        SELECT COUNT(*)
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1 AND gr.status = 'active'
    """, story["id"])
    if not res_count:
        client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text=(
                f"*{story['title']}* has no resources allocated yet — "
                f"the assistant can't plan against an empty RESOURCES.md.\n\n"
                f"Ask the Goal Orchestrator in this channel to grant "
                f"the repo + working branch (and any docs) to this story "
                f"via `grant_resource`, then try `/work` again."
            ),
        )
        _log.info(
            "Slack /work blocked: story=%s has zero allocated resources "
            "(uid=%s goal=%s)",
            story["id"], user_uid, goal_id,
        )
        return

    # /work means the user is taking the story now: mark it accepted, then
    # import (provision) it directly. (Board "Accept" only marks accepted; it no
    # longer provisions — so Slack does both steps here.) Idempotent: only
    # proposed -> accepted, never resets an in-flight story.
    await pool.execute(
        "UPDATE stories SET state='accepted', updated_at=now() "
        "WHERE id=$1 AND state='proposed'",
        story["id"],
    )

    # Resolve the persistent (user × goal) SHCA thread + provision agent.
    # On first call this also creates the agent and the persistent thread;
    # on subsequent calls it reuses both, appending a fresh "Story Mode
    # Active" marker so the conversation rotates onto the new story.
    try:
        from app.routers.assistant import provision_assistant_thread_for_story
        thread_id, agent_id = await provision_assistant_thread_for_story(
            user_uid, goal_id, story["id"],
        )
    except Exception as exc:
        _log.error("Failed to provision SHCA thread for Slack story flow: %s", exc, exc_info=True)
        client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text="Sorry — I couldn't set up your assistant. Please try again from the control panel.",
        )
        return

    # Look up the SHCA's display name + template_id for the channel name.
    user_row = await pool.fetchrow(
        "SELECT display_name, email FROM users WHERE uid = $1", user_uid,
    )
    user_display_name = (
        (user_row["display_name"] if user_row else "")
        or (user_row["email"].split("@")[0] if user_row and user_row.get("email") else "")
        or user_uid
    )
    from app.services.agent_registry.assistant_queries import get_user_assistant_template_id_by_uid
    agent_template_id = (await get_user_assistant_template_id_by_uid(pool, user_uid)) or "shca"

    # Run channel creation and gateway provisioning IN PARALLEL.
    # Previously these were sequential: channel first, then kickoff
    # (which lazily triggered ensure_shca_session, which kicked off
    # ~30-60s of resource provisioning before the SHCA could reply).
    # By starting ensure_shca_session as soon as we have the thread id,
    # the gateway is provisioning the workspace WHILE the Slack channel
    # is being created and the welcome posted. By the time the user
    # opens the new channel the SHCA is much closer to ready.
    from app.services.agent_driver import ensure_shca_session
    session_task = asyncio.create_task(
        ensure_shca_session(goal_id, agent_id, thread_id),
    )

    # Ensure the (user × goal) Slack channel exists; rotate current_story.
    try:
        mapping = await ensure_user_goal_channel(
            goal_id=goal_id,
            user_uid=user_uid,
            user_display_name=user_display_name,
            user_slack_id=slack_user,
            thread_id=thread_id,
            agent_id=agent_id,
            agent_template_id=agent_template_id,
            bot_token=bot_token,
            story=dict(story),
        )
    except Exception:
        # Make sure the parallel session task doesn't get stranded if
        # channel creation throws. Drain it (best-effort) so callers
        # that follow can still rely on a clean state.
        session_task.cancel()
        raise
    if not mapping:
        session_task.cancel()
        client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text="I created the assistant but couldn't create a Slack channel. "
                 "Please try from the control panel.",
        )
        return

    # Wait for gateway session creation to finish. By this point the
    # Slack channel exists + welcome has posted; if session creation
    # is already done the await is instant, otherwise we wait the
    # remaining time.
    try:
        await session_task
    except Exception as exc:
        _log.warning(
            "ensure_shca_session (parallel) failed for goal=%s thread=%s: %s",
            goal_id, thread_id[:12] if thread_id else "-", exc,
        )
        # Continue — refresh_story_resources below will retry the
        # provisioning steps and we'll get a clearer error there if
        # it's still broken.

    client.chat_postEphemeral(
        channel=goal_channel_id, user=slack_user,
        text=(
            f"Now working on *{story['title']}* in <#{mapping['slack_channel_id']}>. "
            f"Your assistant is reading the story brief and will start planning."
        ),
    )

    # CRITICAL gate: ensure RESOURCES.md is refreshed for THIS story
    # before the kickoff fires. Two cases:
    #   - Fresh session (first /work in this user-goal): _provision_resources
    #     ran during session creation and wrote RESOURCES.md against the
    #     active story. refresh_story_resources is effectively idempotent.
    #   - Story rotation (existing session, new story): the in-memory
    #     session already has stale RESOURCES.md from the previous
    #     story. ensure_shca_session returned the existing session
    #     immediately without rewriting files. Without this refresh
    #     the SHCA reads the old brief, hits the "stale resources"
    #     safety rail in AGENTS.md, and stops.
    # Awaiting this gate means the kickoff is sent only after
    # RESOURCES.md (and IDENTITY.md) reflect the current story.
    try:
        from app.resource_provisioning import refresh_story_resources
        refreshed = await refresh_story_resources(goal_id, story["id"])
        if not refreshed:
            _log.info(
                "refresh_story_resources returned False for goal=%s story=%s — "
                "session may not be in memory yet; the kickoff send below "
                "will still trigger correct provisioning on first chat.send",
                goal_id, story["id"],
            )
    except Exception as exc:
        _log.warning(
            "refresh_story_resources failed pre-kickoff (goal=%s story=%s): %s — "
            "proceeding with kickoff dispatch anyway",
            goal_id, story["id"], exc,
        )

    # Final stage: auto-kickoff. Sends a short, user-style message
    # ("Let's get started on …") to the SHCA so it begins planning.
    # Attributed in the ledger to the user who ran /work
    # (kind="story_assignment_kickoff", trigger="auto_after_provisioning").
    # Runs only after RESOURCES.md has been refreshed for THIS story
    # (gate above), so the SHCA never sees stale resources.
    try:
        from app.routers.assistant import dispatch_story_kickoff_to_assistant
        await dispatch_story_kickoff_to_assistant(
            goal_id=goal_id,
            story_id=story["id"],
            thread_id=thread_id,
            agent_id=agent_id,
            user_uid=user_uid,
        )
    except Exception as exc:
        _log.warning(
            "Story-kickoff dispatch failed for goal=%s story=%s: %s",
            goal_id, story["id"], exc,
        )


# ── member_joined_channel ────────────────────────────────────────────────────

async def _handle_member_joined_channel(team_id: str, event: dict) -> None:
    """A Slack user was added to a mapped goal / orch / story channel.

    Policy:
      - If the new member's email matches a row in `users`, add them to the
        goal (INSERT into goal_members) and send a welcome DM.
      - Otherwise, kick them from the channel and DM them an explanation.
      - The bot's own joins (from conversations_join after channel creation)
        are ignored.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    channel = event.get("channel", "")
    slack_user_id = event.get("user", "")
    if not channel or not slack_user_id:
        return

    # Figure out which goal this channel belongs to + which kind of channel.
    goal_id = ""
    kind = ""
    config = await get_goal_by_channel(team_id, channel)
    if config:
        goal_id = config["goal_id"]
        kind = "goal"
    else:
        orch = await get_goal_by_orch_channel(team_id, channel)
        if orch:
            goal_id = orch["goal_id"]
            kind = "orch"
            config = orch
        else:
            ugc_mapping = await ugc.get_by_channel_id(channel)
            if ugc_mapping and not ugc_mapping.get("archived_at"):
                goal_id = ugc_mapping["goal_id"]
                kind = "user_goal"
                config = await get_goal_slack_config(goal_id)
            else:
                mapping = await story_ch.get_by_channel_id(channel)
                if mapping and not mapping.get("archived_at"):
                    goal_id = mapping["goal_id"]
                    kind = "story"
                    config = await get_goal_slack_config(goal_id)

    if not goal_id or not config:
        _log.debug("member_joined_channel: channel %s not mapped — ignoring", channel)
        return

    bot_token = config.get("bot_token", "")
    bot_user_id = config.get("bot_user_id", "") or ""

    # Bot joining its own newly-created channel — nothing to do.
    if slack_user_id == bot_user_id:
        return

    client = WebClient(token=bot_token)

    # Fetch profile (email + display name) — profile is the only reliable way
    # to map to our `users` table.
    try:
        info = client.users_info(user=slack_user_id)
    except SlackApiError as exc:
        _log.warning(
            "member_joined_channel: users_info failed for %s: %s",
            slack_user_id, exc,
        )
        return
    profile = (info.get("user") or {}).get("profile", {}) or {}
    email = (profile.get("email") or "").strip()
    display_name = (profile.get("display_name") or profile.get("real_name") or "").strip()

    if not email:
        await _kick_and_warn(
            client, channel, slack_user_id,
            reason=(
                "I can't see your email (workspace policy may hide it) and I "
                "can only admit members whose email matches a Control Panel "
                "account. Ask your admin to grant the bot `users:read.email` "
                "or invite you in the Control Panel first."
            ),
            channel_warning=(
                f":warning: Removed <@{slack_user_id}> — I couldn't read their "
                f"email from Slack, so I can't verify they have a Control Panel "
                f"account. (Workspace may be hiding emails from apps.)"
            ),
            log_context=f"channel={channel} kind={kind} (no email)",
        )
        return

    # Look up in our users table
    pool = get_pool()
    matched = await pool.fetchrow(
        "SELECT uid, display_name FROM users WHERE email = $1", email,
    )
    if not matched:
        await _kick_and_warn(
            client, channel, slack_user_id,
            reason=(
                f"You were added to a goal channel, but your Slack email "
                f"`{email}` is not registered in the Control Panel. "
                f"Sign up at the Control Panel first (same email), then ask "
                f"to be re-added to this channel."
            ),
            channel_warning=(
                f":warning: Removed <@{slack_user_id}> — their email "
                f"`{email}` is not registered in the Control Panel. "
                f"Ask them to sign up with the same email, then re-add."
            ),
            log_context=f"channel={channel} kind={kind} email={email}",
        )
        return

    uid = matched["uid"]
    name = matched.get("display_name") or display_name or email

    # Map the Slack user ↔ uid so future messages resolve instantly.
    try:
        from app.slack.thread_map import map_slack_user
        await map_slack_user(
            slack_user_id, team_id, uid,
            slack_display_name=display_name, slack_email=email,
        )
    except Exception as exc:
        _log.warning("member_joined_channel: map_slack_user failed: %s", exc)

    # Add them to goal_members — idempotent on conflict.
    try:
        await pool.execute("""
            INSERT INTO goal_members (goal_id, uid, role, added_by)
            VALUES ($1, $2, 'member', $2)
            ON CONFLICT (goal_id, uid) DO NOTHING
        """, goal_id, uid)
    except Exception as exc:
        _log.warning(
            "member_joined_channel: goal_members INSERT failed goal=%s uid=%s: %s",
            goal_id, uid, exc,
        )

    # Friendly welcome ephemeral in-channel.
    try:
        client.chat_postEphemeral(
            channel=channel, user=slack_user_id,
            text=(
                f":wave: Welcome, *{name}* — you've been added to this goal. "
                f"Use `@project-assistant work on <story>` when a story is "
                f"assigned to you."
            ),
        )
    except SlackApiError as exc:
        _log.warning("Welcome ephemeral failed: %s", exc)

    _log.info(
        "Slack member admitted: goal=%s kind=%s uid=%s email=%s slack_user=%s",
        goal_id, kind, uid, email, slack_user_id,
    )


async def _kick_and_warn(
    client, channel: str, slack_user_id: str,
    reason: str, channel_warning: str, log_context: str,
) -> None:
    """Remove a Slack user from the channel, DM them the reason, and post
    a visible warning in the channel so other members know why.

    Order: DM first (so the kicked user sees context), then kick, then
    channel warning (so other members see the removal explanation after
    Slack's own "X was removed" system line)."""
    from slack_sdk.errors import SlackApiError

    # 1. DM the kicked user with the long-form reason.
    dm_channel = ""
    try:
        resp = client.conversations_open(users=slack_user_id)
        dm_channel = resp.get("channel", {}).get("id", "")
    except SlackApiError as exc:
        _log.warning("conversations.open failed for %s: %s", slack_user_id, exc)

    if dm_channel:
        try:
            client.chat_postMessage(
                channel=dm_channel,
                text=f":warning: You were added to <#{channel}> but I had to remove you.\n\n{reason}",
            )
        except SlackApiError as exc:
            _log.warning("DM warning failed for %s: %s", slack_user_id, exc)

    # 2. Kick from the channel.
    kicked = False
    try:
        client.conversations_kick(channel=channel, user=slack_user_id)
        kicked = True
        _log.info(
            "Kicked unverified Slack user %s from channel %s (%s)",
            slack_user_id, channel, log_context,
        )
    except SlackApiError as exc:
        # not_allowed_token_type / cant_kick_self / etc.
        _log.warning(
            "conversations.kick failed for %s in %s: %s",
            slack_user_id, channel, exc.response.get("error", str(exc)),
        )

    # 3. Post a public warning in the channel so other members see why
    #    the user was removed (Slack's system line "X was removed" has no
    #    explanation of its own).
    if kicked and channel_warning:
        try:
            client.chat_postMessage(
                channel=channel, text=channel_warning, username="System",
            )
        except SlackApiError as exc:
            _log.warning(
                "Channel warning post failed (channel=%s): %s",
                channel, exc.response.get("error", str(exc)),
            )


# ── File uploads ─────────────────────────────────────────────────────────────

async def _handle_file_uploads(
    goal_id: str,
    channel: str,
    slack_user: str,
    user_uid: str,
    files: list[dict],
    bot_token: str,
    story_id: str = "",
) -> None:
    """Download each shared file from Slack and register it as a goal_resource.

    Posts an ephemeral per-file result message so the uploader sees what
    worked and what didn't without spamming the channel.
    """
    import httpx
    from slack_sdk import WebClient

    from app.services.resource_upload import (
        ResourceUploadError,
        upload_and_register_resource,
        ALLOWED_FILE_TYPES,
        MAX_UPLOAD_SIZE,
    )

    pool = get_pool()
    user_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", user_uid,
    )

    client = WebClient(token=bot_token)

    for f in files:
        file_id = f.get("id", "")
        name = f.get("name", "") or f.get("title", "") or file_id
        size = int(f.get("size", 0) or 0)
        url = f.get("url_private_download") or f.get("url_private", "")
        mode = f.get("mode", "")

        # Skip non-downloadable files (snippets stored client-side, tombstones)
        if mode == "tombstone":
            continue
        if not url:
            _log.info("Skipping file with no private URL: %s", file_id)
            continue

        # Extension guard up-front so we don't burn bandwidth on rejected types
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_FILE_TYPES:
            _post_ephemeral(
                client, channel, slack_user,
                f"Skipped *{name}* — file type `.{ext or '?'}` isn't supported.",
            )
            continue
        if size and size > MAX_UPLOAD_SIZE:
            _post_ephemeral(
                client, channel, slack_user,
                f"Skipped *{name}* — exceeds {MAX_UPLOAD_SIZE // 1024 // 1024} MB limit.",
            )
            continue

        try:
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.get(
                    url, headers={"Authorization": f"Bearer {bot_token}"},
                )
                if resp.status_code != 200:
                    _log.warning(
                        "Slack file download failed: file=%s status=%d",
                        file_id, resp.status_code,
                    )
                    _post_ephemeral(
                        client, channel, slack_user,
                        f"Couldn't download *{name}* from Slack (HTTP {resp.status_code}).",
                    )
                    continue
                file_bytes = resp.content
        except Exception as exc:
            _log.warning("Slack file download error (file=%s): %s", file_id, exc)
            _post_ephemeral(
                client, channel, slack_user,
                f"Couldn't download *{name}*: {exc}",
            )
            continue

        try:
            result = await upload_and_register_resource(
                goal_id=goal_id,
                filename=name,
                file_bytes=file_bytes,
                uploader_uid=user_uid,
                uploader_actor_id=str(user_actor_id) if user_actor_id else None,
                human_context=f"Uploaded from Slack channel <#{channel}>",
            )
        except ResourceUploadError as exc:
            _post_ephemeral(
                client, channel, slack_user, f"Skipped *{name}*: {exc}",
            )
            continue
        except Exception as exc:
            _log.error("upload_and_register_resource failed for %s: %s", name, exc, exc_info=True)
            _post_ephemeral(
                client, channel, slack_user,
                f"Couldn't register *{name}* as a goal resource.",
            )
            continue

        resource_id = result.resource_id
        is_new = result.is_new

        # Link to story if the file was uploaded in a story channel.
        # ON CONFLICT DO NOTHING handles the "already linked" case from
        # webhook retries.
        if story_id:
            try:
                await pool.execute("""
                    INSERT INTO story_resources (story_id, resource_id, relation)
                    VALUES ($1, $2, 'needs')
                    ON CONFLICT DO NOTHING
                """, story_id, resource_id)
            except Exception as exc:
                _log.warning(
                    "Failed to link resource %s to story %s: %s",
                    resource_id, story_id, exc,
                )

        # Push the new doc into every live agent session's workspace AFTER
        # the story_resources link lands, so the SHCA's story-scoped
        # download_documents_to_workspace query sees the link and pushes
        # the file. GO is goal-wide so it gets the file either way.
        try:
            import asyncio as _asyncio
            from app.resource_provisioning import refresh_all_goal_sessions
            _asyncio.create_task(refresh_all_goal_sessions(goal_id))
        except Exception as exc:
            _log.warning(
                "refresh_all_goal_sessions failed for goal %s: %s", goal_id, exc,
            )

        # Only announce on first registration — silently skip on duplicate
        # webhook retries so the user doesn't see confusing double-messages.
        if is_new:
            _post_ephemeral(
                client, channel, slack_user,
                f"📎 Registered *{name}* as a goal resource (`{resource_id}`).",
            )
            _log.info(
                "Slack file registered: goal=%s story=%s resource=%s size=%d name=%r",
                goal_id, story_id or "-", resource_id, len(file_bytes), name,
            )
        else:
            _log.info(
                "Slack file upload was a dupe (already registered): "
                "goal=%s resource=%s name=%r — no ephemeral posted",
                goal_id, resource_id, name,
            )


def _post_ephemeral(client, channel: str, user: str, text: str) -> None:
    """Best-effort chat.postEphemeral — swallow errors."""
    try:
        client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception as exc:
        _log.warning("Failed to post ephemeral: %s", exc)


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_user(
    team_id: str, slack_user_id: str, bot_token: str, channel_id: str = "",
) -> str:
    """Resolve Slack user → control panel uid. Auto-maps by email on first encounter."""
    if not slack_user_id:
        return ""
    uid = await get_user_uid(slack_user_id, team_id)
    if uid:
        return uid

    try:
        client = WebClient(token=bot_token)
        info = client.users_info(user=slack_user_id)
        profile = info.get("user", {}).get("profile", {})
        email = profile.get("email", "")
        display_name = profile.get("display_name", "") or profile.get("real_name", "")
        if email:
            uid = await auto_map_user_by_email(
                slack_user_id, team_id, email, display_name,
            )
            if uid:
                _log.info(
                    "Auto-mapped Slack user %s (%s) → uid=%s",
                    slack_user_id, email, uid,
                )
                return uid
    except Exception as exc:
        _log.warning("Failed to resolve Slack user %s: %s", slack_user_id, exc)

    # Unmapped — send signup prompt ephemerally
    if channel_id:
        try:
            client = WebClient(token=bot_token)
            client.chat_postEphemeral(
                channel=channel_id, user=slack_user_id,
                text="I don't recognize your account. Please sign in to the control panel "
                     "with the same email as your Slack account, then try again.",
            )
        except Exception:
            pass
    return ""


def _match_story(stories, text: str):
    """Try to match a story by title substring, then by short id prefix."""
    if not text:
        return None
    t = text.lower()
    for s in stories:
        title = s["title"].lower()
        if title in t or t in title:
            return dict(s)
    for s in stories:
        if s["id"][:8] in text:
            return dict(s)
    return None


def _ephemeral_story_list(client, channel_id: str, slack_user: str, stories, user_uid: str):
    """Send an ephemeral list of stories available to the user."""
    mine = [s for s in stories if s["assigned_to"] == user_uid]
    rest = [s for s in stories if s["assigned_to"] != user_uid]
    lines = ["I couldn't identify which story you want to work on."]
    if mine:
        lines.append("\n*Assigned to you:*")
        lines.extend(
            f"• *{s['title']}* (`{s['id'][:8]}`) — {s['state']}" for s in mine[:10]
        )
    if rest:
        lines.append("\n*Other stories in this goal:*")
        lines.extend(
            f"• *{s['title']}* (`{s['id'][:8]}`) — {s['state']}" for s in rest[:10]
        )
    if not stories:
        lines.append("No active stories in this goal.")
    try:
        client.chat_postEphemeral(
            channel=channel_id, user=slack_user, text="\n".join(lines),
        )
    except SlackApiError as exc:
        _log.warning("Failed to post story list ephemeral: %s", exc)


async def _unmet_dependencies(story) -> list[dict]:
    """Return deps that are still blocking — i.e. not in a terminal state.

    Terminal states (done/cancelled/rejected) never block: if a dep was
    cancelled the plan was revised; forcing downstream to wait forever
    punishes the user for that revision.
    """
    deps = story.get("depends_on") or []
    if not deps:
        return []
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT id, title, state FROM stories
         WHERE id = ANY($1) AND state NOT IN ('done', 'cancelled', 'rejected')
    """, list(deps))
    return [dict(r) for r in rows]


async def _post_paused_notice(bot_token: str, channel: str, scope: str) -> None:
    """Best-effort: tell the Slack channel their message was dropped by the kill
    switch. Never raises — a failed notice must not break inbound handling.

    Lives on the legacy /slack/* path, which has no other freeze awareness; the
    generic channels/slack adapter posts its own equivalent notice."""
    if not bot_token or not channel:
        return
    try:
        WebClient(token=bot_token).chat_postMessage(
            channel=channel,
            text=(f"⏸ Execution is paused ({scope}). "
                  "Resume in the control panel to continue."),
        )
    except Exception as exc:
        _log.warning("Slack paused-notice post failed (channel=%s): %s", channel, exc)


async def _route_to_go(goal_id: str, user_uid: str, text: str,
                       channel: str = "", bot_token: str = "") -> None:
    """Route a message to the GO session. Starts the session headlessly if idle."""
    pool = get_pool()

    # Kill switch — conversational freeze gate. Drop the turn BEFORE persisting
    # (so a frozen goal never accrues an orphan 'human' row the GO silently
    # misses on resume) and tell the Slack user. This is the only freeze check on
    # the legacy /slack/* path; it mirrors the new channels/slack adapter.
    from app.services import execution_control  # noqa: PLC0415
    _block = await execution_control.conversation_block(goal_id)
    if _block is not None:
        _log.info("Legacy Slack→GO dropped (frozen scope=%s) goal=%s", _block.scope, goal_id)
        await _post_paused_notice(bot_token, channel, _block.scope)
        return

    goal = await pool.fetchrow(
        "SELECT go_thread_id, go_agent_id FROM goals WHERE id = $1", goal_id,
    )
    if not goal or not goal.get("go_thread_id"):
        _log.warning("No GO thread for goal %s", goal_id)
        return

    go_thread_id = goal["go_thread_id"]

    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
           VALUES ($1, $2, $3, 'human', $4, 0)""",
        msg_id, go_thread_id, user_uid, text,
    )

    # Push the Slack-originated message to any connected goal-thread UI
    # subscribers so they see it in real time (matching the surface they
    # already use for in-UI typing).
    try:
        display = await pool.fetchval(
            "SELECT display_name FROM users WHERE uid = $1", user_uid,
        )
        from app.routers.goal_thread import _broadcast_to_goal_thread
        await _broadcast_to_goal_thread(goal_id, json.dumps({
            "type": "message",
            "id": msg_id,
            "sender": user_uid,
            "sender_name": display or "",
            "role": "human",
            "content": text,
            "origin": "slack",
        }))
    except Exception as exc:
        _log.warning("UI broadcast for Slack→GO failed (goal=%s): %s", goal_id, exc)

    from app.services.agent_driver import ensure_go_session
    session = await ensure_go_session(goal_id)
    if not session:
        _log.warning("Could not start GO session for goal %s", goal_id)
        return

    await session.send_to_gateway(json.dumps({
        "type": "message",
        "content": text,
        "thread_id": go_thread_id,
    }))
    _log.info("Routed Slack message to GO: goal=%s uid=%s", goal_id, user_uid)


async def _route_to_shca(
    goal_id: str, thread_id: str, agent_id: str, user_uid: str, text: str,
    channel: str = "", bot_token: str = "",
) -> None:
    """Route a message to an SHCA session. Starts the session headlessly if idle."""
    pool = get_pool()

    # Kill switch — conversational freeze gate (see _route_to_go). Drop BEFORE
    # persisting so a frozen goal never accrues an orphan 'human' row, and notify
    # the Slack user. Only freeze check on the legacy /slack/* path.
    from app.services import execution_control  # noqa: PLC0415
    _block = await execution_control.conversation_block(goal_id)
    if _block is not None:
        _log.info("Legacy Slack→SHCA dropped (frozen scope=%s) goal=%s thread=%s",
                  _block.scope, goal_id, thread_id[:12])
        await _post_paused_notice(bot_token, channel, _block.scope)
        return

    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
           VALUES ($1, $2, $3, 'human', $4, 0)""",
        msg_id, thread_id, user_uid, text,
    )

    from app.services.agent_driver import ensure_shca_session
    session = await ensure_shca_session(goal_id, agent_id, thread_id)
    if not session:
        _log.warning(
            "Could not start SHCA session: goal=%s agent=%s thread=%s",
            goal_id, agent_id, thread_id[:12],
        )
        return

    # Push the Slack-originated message to any connected SHCA chat UI
    # subscribers on this thread so it appears in real time. The UI
    # doesn't render this for in-browser typing (it appends locally on
    # send), so there's no echo to suppress.
    try:
        display = await pool.fetchval(
            "SELECT display_name FROM users WHERE uid = $1", user_uid,
        )
        await session._broadcast(thread_id, json.dumps({
            "type": "human_message",
            "id": msg_id,
            "sender": user_uid,
            "sender_name": display or "",
            "content": text,
            "origin": "slack",
        }))
    except Exception as exc:
        _log.warning(
            "UI broadcast for Slack→SHCA failed (goal=%s thread=%s): %s",
            goal_id, thread_id[:12], exc,
        )

    await session.send_to_gateway(json.dumps({
        "type": "message",
        "content": text,
        "thread_id": thread_id,
    }))
    _log.info(
        "Routed Slack message to SHCA: goal=%s thread=%s agent=%s",
        goal_id, thread_id[:12], agent_id,
    )
