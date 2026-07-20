"""DEPRECATED — superseded by app/channels/slack/lifecycle.py and provisioner.py.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Lifecycle hooks — auto-create Slack channels on goal/story events.

Channel-per-story model (v2):
  Goal gets Slack enabled → create #<goal-slug> + #<goal-slug>-orchestrator channels
  User asks to work on a story (inbound flow) → create #<goal-slug>-<story-slug> PRIVATE
  Story done/cancelled/rejected → archive the story channel
"""
import logging
import re
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.slack.config import (
    get_goal_slack_config,
    upsert_goal_slack_config,
)
from app.slack import story_channels as story_ch
from app.slack import user_goal_channels as ugc

_log = logging.getLogger(__name__)


def _slugify(name: str, max_len: int = 80) -> str:
    """Convert a name to a Slack-safe channel slug.
    Slack channel names: lowercase, no spaces, max 80 chars, only a-z0-9-_.
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s\-_]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len] or "x"


def _find_channel_by_name(client: WebClient, name: str, is_private: bool = False) -> Optional[str]:
    """Find a channel by name across public and private listings. Returns channel_id or None."""
    types = "private_channel" if is_private else "public_channel"
    cursor = None
    for _ in range(10):  # paginate up to 2000 channels
        resp = client.conversations_list(types=types, limit=200, cursor=cursor)
        for ch in resp.get("channels", []):
            if ch.get("name") == name:
                return ch.get("id")
        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
    return None


def _create_or_join_public_channel(client: WebClient, name: str) -> str:
    """Create a public channel; if name_taken, find and join it. Returns channel_id."""
    try:
        result = client.conversations_create(name=name, is_private=False)
        channel_id = result["channel"]["id"]
        _log.info("Created public channel #%s (%s)", name, channel_id)
    except SlackApiError as exc:
        if exc.response.get("error") == "name_taken":
            _log.info("Channel #%s already exists, looking it up", name)
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


def _create_private_channel(client: WebClient, name: str) -> str:
    """Create a private channel. On name collision, append -2, -3, ... up to -9."""
    for suffix in ("", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"):
        candidate = f"{name}{suffix}"[:80]
        try:
            result = client.conversations_create(name=candidate, is_private=True)
            channel_id = result["channel"]["id"]
            _log.info("Created private channel #%s (%s)", candidate, channel_id)
            return channel_id
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err == "name_taken":
                continue
            raise
    raise RuntimeError(f"Could not create private channel after 9 attempts from base {name}")


async def _invite_goal_members_to_channels(
    client: WebClient,
    goal_id: str,
    workspace_id: str,
    goal_channel_id: str,
    orch_channel_id: str,
) -> int:
    """Invite every goal_members row (with a Slack account matching their
    Control Panel email) to the two shared channels.

    Returns the count of distinct members who ended up in the goal
    channel (either newly invited or already there). Persists the
    resolved slack_user_id → uid mappings into slack_user_map so later
    Slack messages from these users resolve instantly without an
    extra Slack API round-trip.
    """
    from app.database import get_pool
    from app.slack.thread_map import map_slack_user

    pool = get_pool()
    members = await pool.fetch(
        """SELECT u.uid, u.email, u.display_name
             FROM goal_members gm
             JOIN users u ON u.uid = gm.uid
            WHERE gm.goal_id = $1
              AND u.email <> ''""",
        goal_id,
    )

    slack_user_ids: list[str] = []
    for m in members:
        email = m["email"]
        try:
            resp = client.users_lookupByEmail(email=email)
            slack_id = (resp.get("user") or {}).get("id", "")
            profile = (resp.get("user") or {}).get("profile") or {}
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc)) if hasattr(exc, "response") else str(exc)
            if err == "users_not_found":
                _log.info(
                    "Slack: goal member %s (%s) not in workspace — skipping invite",
                    m.get("display_name") or m["uid"], email,
                )
            else:
                _log.warning("users.lookupByEmail failed for %s: %s", email, err)
            continue
        except Exception as exc:
            _log.warning("users.lookupByEmail error for %s: %s", email, exc)
            continue

        if not slack_id:
            continue

        slack_user_ids.append(slack_id)
        try:
            await map_slack_user(
                slack_user_id=slack_id,
                workspace_id=workspace_id,
                user_uid=m["uid"],
                slack_display_name=(
                    profile.get("display_name") or profile.get("real_name") or ""
                ),
                slack_email=email,
            )
        except Exception as exc:
            _log.debug("map_slack_user failed for %s: %s", slack_id, exc)

    if not slack_user_ids:
        return 0

    # Slack's conversations.invite takes up to 1000 comma-separated users.
    users_csv = ",".join(slack_user_ids)

    for channel_id, label in (
        (goal_channel_id, "goal"),
        (orch_channel_id, "orch"),
    ):
        if not channel_id:
            continue
        try:
            client.conversations_invite(channel=channel_id, users=users_csv)
            _log.info(
                "Invited %d members to %s channel %s",
                len(slack_user_ids), label, channel_id,
            )
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc)) if hasattr(exc, "response") else str(exc)
            # `already_in_channel` is returned when at least one user was
            # already in — we don't know whose specifically, but Slack
            # invites the rest in the same call. Treat as success.
            if err in ("already_in_channel", "cant_invite_self"):
                _log.info(
                    "Partial invite to %s channel (some already present): %s",
                    label, err,
                )
            else:
                _log.warning(
                    "conversations.invite failed for %s channel %s: %s",
                    label, channel_id, err,
                )

    return len(slack_user_ids)


# ── Goal Slack setup ─────────────────────────────────────────────────────────

async def on_goal_slack_enabled(
    goal_id: str,
    goal_name: str,
    workspace_id: str,
    bot_token: str,
    channel_name: str = "",
    existing_channel_id: str = "",
) -> dict:
    """Called when an admin enables Slack for a goal.

    Creates (or joins) the goal channel + the orchestrator channel.
    No thread parents — the channels themselves are the conversations.

    Returns: {goal_channel_id, orch_channel_id, goal_channel_name, orch_channel_name}
    """
    client = WebClient(token=bot_token)

    # 1. Goal channel
    goal_slug = _slugify(channel_name or goal_name)
    if existing_channel_id:
        goal_channel_id = existing_channel_id
        try:
            client.conversations_join(channel=goal_channel_id)
        except SlackApiError as exc:
            if exc.response.get("error") != "already_in_channel":
                _log.warning("Failed to join existing channel %s: %s", goal_channel_id, exc)
        try:
            info = client.conversations_info(channel=goal_channel_id)
            goal_slug = info["channel"]["name"]
        except Exception:
            pass
    else:
        goal_channel_id = _create_or_join_public_channel(client, goal_slug)

    # Resolve the actual channel name back from Slack (in case we joined existing)
    try:
        info = client.conversations_info(channel=goal_channel_id)
        goal_channel_name = info["channel"]["name"]
    except Exception:
        goal_channel_name = goal_slug

    # 2. Orchestrator channel
    orch_slug = _slugify(f"{goal_slug}-orchestrator")
    orch_channel_id = _create_or_join_public_channel(client, orch_slug)
    try:
        info = client.conversations_info(channel=orch_channel_id)
        orch_channel_name = info["channel"]["name"]
    except Exception:
        orch_channel_name = orch_slug

    # 3. Post welcome in goal channel
    try:
        client.chat_postMessage(
            channel=goal_channel_id,
            text=(
                f"*Goal:* {goal_name}\n"
                f"Chat here to talk to the Goal Orchestrator.\n"
                f"Say `@project-assistant work on <story>` to start working on a story "
                f"you've been assigned."
            ),
            username="Goal Orchestrator",
        )
    except SlackApiError as exc:
        _log.warning("Failed to post goal welcome: %s", exc)

    # 4. Post notice in orchestrator channel
    try:
        client.chat_postMessage(
            channel=orch_channel_id,
            text=(
                ":information_source: *Orchestrator channel (read-only).*\n"
                "The Goal Orchestrator and assistants coordinate here. "
                "Human messages are deleted — use the goal channel to talk to the GO."
            ),
            username="System",
        )
    except SlackApiError as exc:
        _log.warning("Failed to post orch notice: %s", exc)

    # 5. Persist config
    await upsert_goal_slack_config(
        goal_id=goal_id,
        workspace_id=workspace_id,
        goal_channel_id=goal_channel_id,
        goal_channel_name=goal_channel_name,
        orch_channel_id=orch_channel_id,
        orch_channel_name=orch_channel_name,
    )

    # 6. Invite every goal member (with a Slack account matching their
    # Control Panel email) to the goal channel and the orchestrator
    # channel. Without this, humans don't see the channel appear in
    # their sidebar — public channels don't auto-appear until joined.
    invited = await _invite_goal_members_to_channels(
        client=client,
        goal_id=goal_id,
        workspace_id=workspace_id,
        goal_channel_id=goal_channel_id,
        orch_channel_id=orch_channel_id,
    )

    _log.info(
        "Slack enabled for goal %s: #%s (%s) + #%s (%s) — invited %d members",
        goal_id, goal_channel_name, goal_channel_id,
        orch_channel_name, orch_channel_id, invited,
    )
    return {
        "goal_channel_id": goal_channel_id,
        "goal_channel_name": goal_channel_name,
        "orch_channel_id": orch_channel_id,
        "orch_channel_name": orch_channel_name,
    }


# ── User-goal channel (post-pivot: one Slack channel per user × goal) ───────

async def ensure_user_goal_channel(
    goal_id: str,
    user_uid: str,
    user_display_name: str,
    user_slack_id: str,
    thread_id: str,
    agent_id: str,
    agent_template_id: str,
    bot_token: str,
    story: Optional[dict] = None,
) -> Optional[dict]:
    """Find or create the (user, goal) Slack channel; rotate the active
    story marker when `story` is provided.

    Naming: <goal_id>-<user-name-slug>-<agent-template-id>
        e.g. bmi12-abhishek-chaudhary-shca

    Idempotent. On repeat call:
      - existing channel is reused (re-invites the user just in case
        they were removed)
      - current_story_id is rotated
      - a "Now working on: <story title>" notice is posted

    Returns the user_goal_channels row, or None on Slack failure.
    """
    client = WebClient(token=bot_token)

    existing = await ugc.get_by_user_goal(goal_id, user_uid)
    if existing and not existing.get("archived_at"):
        channel_id = existing["slack_channel_id"]
        # Re-invite — silent if already a member
        try:
            client.conversations_invite(channel=channel_id, users=user_slack_id)
        except SlackApiError as exc:
            if exc.response.get("error", "") not in (
                "already_in_channel", "user_is_bot", "cant_invite_self",
            ):
                _log.warning(
                    "ensure_user_goal_channel: invite failed for %s/%s: %s",
                    user_slack_id, channel_id, exc,
                )
        # Reflect the active-story rotation in the DB row + post a notice
        if story:
            await ugc.set_current_story(goal_id, user_uid, story["id"])
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    text=(
                        f":arrows_counterclockwise: *Now working on:* "
                        f"{story['title']}\n_The assistant is reading "
                        f"RESOURCES.md and will start planning shortly._"
                    ),
                    username="System",
                )
            except SlackApiError as exc:
                _log.warning("Failed to post story-rotate notice: %s", exc)
        return existing

    # First time: create the channel.
    channel_name = ugc.channel_name_for(goal_id, user_display_name, agent_template_id)
    try:
        channel_id = _create_private_channel(client, channel_name)
    except SlackApiError as exc:
        _log.error(
            "ensure_user_goal_channel: failed to create channel %s: %s",
            channel_name, exc,
        )
        return None
    except Exception as exc:
        _log.error(
            "ensure_user_goal_channel: failed to create channel %s: %s",
            channel_name, exc, exc_info=True,
        )
        return None

    try:
        info = client.conversations_info(channel=channel_id)
        final_name = info["channel"]["name"]
    except Exception:
        final_name = channel_name

    try:
        client.conversations_invite(channel=channel_id, users=user_slack_id)
    except SlackApiError as exc:
        if exc.response.get("error", "") not in (
            "already_in_channel", "user_is_bot",
        ):
            _log.warning(
                "Failed to invite user %s to new channel %s: %s",
                user_slack_id, channel_id, exc,
            )

    mapping = await ugc.upsert(
        goal_id=goal_id,
        user_uid=user_uid,
        slack_channel_id=channel_id,
        slack_channel_name=final_name,
        thread_id=thread_id,
        agent_id=agent_id,
    )
    if story:
        await ugc.set_current_story(goal_id, user_uid, story["id"])

    # Welcome message
    try:
        if story:
            desc = (story.get("description") or "")[:500]
            parts = [
                f"*Welcome.* This is your assistant channel for this goal.",
                f"*Now working on:* {story['title']}",
            ]
            if desc:
                parts.append(f"\n{desc}")
            parts.append(
                "\n_The assistant has loaded the story context and will start "
                "planning shortly. Stories rotate through this channel — your "
                "history persists for the lifetime of the goal._"
            )
            text = "\n".join(parts)
        else:
            text = (
                "*Welcome.* This is your assistant channel for this goal. "
                "Ask the Goal Orchestrator to assign you a story to start "
                "working on it."
            )
        client.chat_postMessage(channel=channel_id, text=text, username="System")
    except SlackApiError as exc:
        _log.warning("Failed to post user-goal-channel welcome: %s", exc)

    return mapping


# ── Story lifecycle (legacy per-story channel — kept for back-compat) ───────

async def create_story_channel(
    goal_id: str,
    goal_slug: str,
    story: dict,
    user_uid: str,
    user_slack_id: str,
    thread_id: str,
    agent_id: str,
    bot_token: str,
) -> Optional[dict]:
    """Create the private story channel, invite the user, persist mapping, post welcome.

    Returns the mapping row, or None on failure.
    """
    client = WebClient(token=bot_token)

    story_slug = _slugify(story["title"])
    # Fit combined length: "<goal>-<story>" ≤ 80
    combined = f"{goal_slug}-{story_slug}"
    if len(combined) > 80:
        budget = 80 - len(goal_slug) - 1
        story_slug = story_slug[:max(1, budget)]
        combined = f"{goal_slug}-{story_slug}"

    try:
        channel_id = _create_private_channel(client, combined)
    except SlackApiError as exc:
        _log.error("Failed to create private story channel %s: %s", combined, exc)
        return None
    except Exception as exc:
        _log.error("Failed to create private story channel %s: %s", combined, exc, exc_info=True)
        return None

    # Resolve final channel name (may have -2, -3 suffix)
    try:
        info = client.conversations_info(channel=channel_id)
        final_name = info["channel"]["name"]
    except Exception:
        final_name = combined

    # Invite the assigned user
    try:
        client.conversations_invite(channel=channel_id, users=user_slack_id)
    except SlackApiError as exc:
        err = exc.response.get("error", "")
        if err not in ("already_in_channel", "user_is_bot"):
            _log.warning("Failed to invite user %s to %s: %s", user_slack_id, channel_id, exc)

    # Persist mapping
    mapping = await story_ch.create_story_channel(
        goal_id=goal_id,
        story_id=story["id"],
        slack_channel_id=channel_id,
        slack_channel_name=final_name,
        user_uid=user_uid,
        thread_id=thread_id,
        agent_id=agent_id,
    )

    # Welcome message
    try:
        desc = story.get("description", "") or ""
        priority = story.get("priority", "") or ""
        parts = [f"*Story:* {story['title']}"]
        if priority and priority != "medium":
            parts.append(f"*Priority:* {priority}")
        if desc:
            parts.append(f"\n{desc[:500]}")
        parts.append(
            "\n_Story moved to in_progress. Your assistant is ready — "
            "ask a question or give a first instruction._"
        )
        client.chat_postMessage(
            channel=channel_id,
            text="\n".join(parts),
            username="System",
        )
    except SlackApiError as exc:
        _log.warning("Failed to post story welcome: %s", exc)

    return mapping


async def on_story_state_changed(
    goal_id: str, story_id: str, old_state: str, new_state: str,
    story_title: str = "",
) -> None:
    """Story state transition.

    Post-pivot semantics:
      - Story channels are no longer per-story. Each user has one
        persistent Slack channel for the goal; stories rotate through
        it via current_story_id.
      - Terminal transitions (done / cancelled / rejected) post a
        completion notice in the user-goal channel and CLEAR
        current_story_id. They do NOT archive the channel — the
        conversation persists for the lifetime of the goal.
      - Non-terminal transitions post an informational status update.

    Legacy per-story channels (slack_story_channels rows) still get
    archived on terminal transitions so old data is tidied up.
    """
    config = await get_goal_slack_config(goal_id)
    if not config or not config.get("enabled"):
        return
    bot_token = config.get("bot_token", "")
    if not bot_token:
        return

    client = WebClient(token=bot_token)

    # ── Post-pivot: notify the user-goal channel that owns this story ──
    from app.database import get_pool
    pool = get_pool()
    ugc_row = await pool.fetchrow("""
        SELECT * FROM slack_user_goal_channels
         WHERE goal_id = $1 AND current_story_id = $2 AND archived_at IS NULL
         LIMIT 1
    """, goal_id, story_id)
    if ugc_row:
        channel_id = ugc_row["slack_channel_id"]
        if new_state in ("done", "cancelled", "rejected"):
            label = {"done": ":white_check_mark: completed",
                     "cancelled": ":no_entry_sign: cancelled",
                     "rejected": ":x: rejected"}[new_state]
            title = story_title or f"`{story_id[:8]}`"
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    text=(
                        f"Story *{title}* {label}. "
                        f"Ask the Goal Orchestrator to assign you the next "
                        f"story when you're ready — this channel stays "
                        f"open for the goal."
                    ),
                    username="System",
                )
            except SlackApiError as exc:
                _log.warning("Failed to post story-done notice: %s", exc)
            await ugc.set_current_story(goal_id, ugc_row["user_uid"], None)
        else:
            state_labels = {
                "proposed": "📋 Proposed",
                "accepted": "✅ Accepted",
                "in_progress": "🔄 In Progress",
                "blocked": "⚠️ Blocked",
            }
            label = state_labels.get(new_state, new_state)
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    text=f"Story status: *{label}*",
                    username="System",
                )
            except SlackApiError as exc:
                _log.warning("Failed to post state change to user-goal channel: %s", exc)

    # ── Legacy per-story channels: archive on terminal as before ──
    legacy = await story_ch.get_story_channel(goal_id, story_id)
    if legacy and not legacy.get("archived_at") and new_state in ("done", "cancelled", "rejected"):
        try:
            client.conversations_archive(channel=legacy["slack_channel_id"])
            await story_ch.mark_archived(goal_id, story_id)
            _log.info(
                "Archived legacy story channel: goal=%s story=%s channel=%s (state=%s)",
                goal_id, story_id, legacy["slack_channel_id"], new_state,
            )
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc)) if hasattr(exc, "response") else str(exc)
            if err == "already_archived":
                await story_ch.mark_archived(goal_id, story_id)
            else:
                _log.warning(
                    "Failed to archive legacy story channel %s: %s",
                    legacy["slack_channel_id"], exc,
                )


# ── Placeholders kept for import compatibility ───────────────────────────────

async def on_story_created(goal_id: str, story: dict) -> Optional[str]:
    """Story created by GO. In channel-model v2, no channel is created until
    the user asks to work on it. We just log for awareness."""
    _log.debug(
        "Slack lifecycle: story created goal=%s story=%s (channel created on user request)",
        goal_id, story.get("id", "?"),
    )
    return None


async def destroy_goal_agent_instances(goal_id: str) -> dict:
    """Thin wrapper around teardown_goal_agents kept for backward compatibility.

    All logic lives in app.services.agent_registry.teardown.teardown_goal_agents.
    Callers outside this module should import that function directly.
    """
    from app.database import get_pool
    from app.services.agent_registry.teardown import teardown_goal_agents
    return await teardown_goal_agents(get_pool(), goal_id)


# Back-compat alias — keep the old name pointing at the new function so
# any out-of-tree callers (e.g. one-off scripts) keep working.
destroy_goal_shca_instances = destroy_goal_agent_instances


async def archive_all_slack_channels_for_goal(goal_id: str) -> dict:
    """On goal deletion: archive every Slack channel owned by this goal.

    Slack's bot API doesn't expose a hard-delete for public or private
    channels (admin.conversations.delete requires Enterprise Grid), so
    archiving is the practical equivalent — the channels become
    read-only and drop out of the normal channel list. Workspace admins
    can still see them in the archived list and manually delete if they
    want a hard purge.

    Order:
      1. Every story channel (active + already-archived story channels
         stay marked archived in the DB — no harm re-archiving).
      2. Orchestrator channel.
      3. Goal channel.
      4. slack_config.enabled = false so the row stops being used for
         routing.

    Returns a summary dict.
    """
    from app.slack.config import disable_goal_slack

    summary = {
        "story_channels": 0,
        "orch_channel": False,
        "goal_channel": False,
        "errors": [],
    }

    config = await get_goal_slack_config(goal_id)
    if not config:
        _log.info(
            "archive_all_slack_channels_for_goal: no slack_config for goal %s — nothing to archive",
            goal_id,
        )
        return summary

    bot_token = config.get("bot_token", "")
    if not bot_token:
        _log.warning(
            "archive_all_slack_channels_for_goal: no bot_token for goal %s", goal_id,
        )
        return summary

    client = WebClient(token=bot_token)

    # 1a. User-goal channels (post-pivot: one per user × goal)
    user_channels = await ugc.list_active_for_goal(goal_id)
    summary["user_goal_channels"] = 0
    for u in user_channels:
        chan = u["slack_channel_id"]
        try:
            client.conversations_archive(channel=chan)
            await ugc.mark_archived(goal_id, u["user_uid"])
            summary["user_goal_channels"] += 1
            _log.info(
                "Archived user-goal channel %s (uid=%s) on goal delete",
                chan, u["user_uid"],
            )
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc)) if hasattr(exc, "response") else str(exc)
            if err == "already_archived":
                await ugc.mark_archived(goal_id, u["user_uid"])
                summary["user_goal_channels"] += 1
            else:
                _log.warning(
                    "Failed to archive user-goal channel %s: %s", chan, err,
                )
                summary["errors"].append(f"user_goal:{chan}:{err}")

    # 1b. Legacy per-story channels (orphaned post-pivot, but a goal that
    # predates the migration may still have them). Archive defensively.
    stories = await story_ch.list_active_for_goal(goal_id)
    for s in stories:
        chan = s["slack_channel_id"]
        try:
            client.conversations_archive(channel=chan)
            await story_ch.mark_archived(goal_id, s["story_id"])
            summary["story_channels"] += 1
            _log.info(
                "Archived story channel %s (story=%s) on goal delete",
                chan, s["story_id"],
            )
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc)) if hasattr(exc, "response") else str(exc)
            if err == "already_archived":
                await story_ch.mark_archived(goal_id, s["story_id"])
                summary["story_channels"] += 1
            else:
                _log.warning(
                    "Failed to archive story channel %s: %s", chan, err,
                )
                summary["errors"].append(f"story:{chan}:{err}")

    # 2. Orchestrator channel
    orch_channel_id = config.get("orch_channel_id", "") or ""
    if orch_channel_id:
        try:
            client.conversations_archive(channel=orch_channel_id)
            summary["orch_channel"] = True
            _log.info("Archived orch channel %s on goal delete", orch_channel_id)
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc))
            if err == "already_archived":
                summary["orch_channel"] = True
            else:
                _log.warning(
                    "Failed to archive orch channel %s: %s", orch_channel_id, err,
                )
                summary["errors"].append(f"orch:{orch_channel_id}:{err}")

    # 3. Goal channel
    goal_channel_id = config.get("goal_channel_id", "") or ""
    if goal_channel_id:
        try:
            client.conversations_archive(channel=goal_channel_id)
            summary["goal_channel"] = True
            _log.info("Archived goal channel %s on goal delete", goal_channel_id)
        except SlackApiError as exc:
            err = exc.response.get("error", str(exc))
            if err == "already_archived":
                summary["goal_channel"] = True
            else:
                _log.warning(
                    "Failed to archive goal channel %s: %s", goal_channel_id, err,
                )
                summary["errors"].append(f"goal:{goal_channel_id}:{err}")

    # 4. Disable the slack_config row so it stops being used for routing
    try:
        await disable_goal_slack(goal_id)
    except Exception as exc:
        _log.warning("disable_goal_slack failed for %s: %s", goal_id, exc)

    _log.info(
        "archive_all_slack_channels_for_goal DONE goal=%s stories=%d orch=%s goal=%s errs=%d",
        goal_id, summary["story_channels"], summary["orch_channel"],
        summary["goal_channel"], len(summary["errors"]),
    )
    return summary


async def on_task_dispatched(goal_id: str, task_id: str, story_id: str = "") -> None:
    _log.debug("Slack lifecycle: task_dispatched %s (stub)", task_id)


async def on_task_completed(goal_id: str, task_id: str, summary: str = "") -> None:
    _log.debug("Slack lifecycle: task_completed %s (stub)", task_id)


async def on_resource_uploaded(goal_id: str, resource_name: str, uploaded_by: str = "") -> None:
    _log.debug("Slack lifecycle: resource_uploaded %s (stub)", resource_name)


async def on_decision_recorded(goal_id: str, decision: str) -> None:
    _log.debug("Slack lifecycle: decision %s (stub)", decision[:60])
