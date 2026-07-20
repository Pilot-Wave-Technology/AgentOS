"""Slack channel provisioner — auto-creates Slack channels when a goal enables Slack.

Implements ChannelProvisioner so the goals.py enable_channel endpoint calls it
automatically. No manual steps required — registering this in channels/registry.py
_PROVISIONERS["slack"] is all that's needed.

Provisioning creates:
  - A public goal channel (#<goal-slug>)
  - A public orchestrator channel (#<goal-slug>-orchestrator)
  - platform_channel_bindings GO row (actor_id IS NULL, thread_id = go_thread_id)
  - Per-member user-goal channels and SHCA bindings (via provision_member)

The Slack bot token comes from platform_integrations.credentials_json (stored
during OAuth install). No imports from app.slack.*.
"""
import logging

from app.channels.base import ChannelProvisioner

_log = logging.getLogger(__name__)


class SlackProvisioner(ChannelProvisioner):
    """Provision Slack channels for goals and members."""

    def is_auto_provisioned(self, config: dict) -> bool:
        """Slack always auto-provisions channels — no manual channel ID entry needed."""
        return True

    def parse_channel_id(self, raw: str) -> str:
        """Accept a Slack channel ID or URL (e.g. https://app.slack.com/client/.../C123).

        Returns the channel ID (C-prefixed string) or empty string if not a valid ID.
        """
        raw = raw.strip()
        if raw.startswith("http"):
            # URL — extract the last path segment
            parts = raw.rstrip("/").split("/")
            for p in reversed(parts):
                if p.startswith("C") and len(p) >= 9:
                    return p
        if raw.startswith("C") and len(raw) >= 9:
            return raw
        return ""

    async def provision_goal(self, goal_id: str, integration_id: str, pool) -> str | None:
        """Create Slack channels for a goal and insert the GO binding. Idempotent.

        Returns an error string on failure, None on success.
        """
        return await _do_provision_goal(goal_id, integration_id, pool)

    async def provision_member(self, goal_id: str, uid: str, pool) -> None:
        """Invite a new member to the goal channel.

        Personal channel creation is attempted immediately if the member already
        has an active SHCA thread for this goal (_do_provision_member skips
        gracefully when none exists, deferring to first story accept/start).
        """
        await _invite_member_to_goal_channel(goal_id, uid, pool)
        await _do_provision_member(goal_id, uid, pool)

    async def deprovision_member(self, goal_id: str, uid: str, pool) -> None:
        """Remove a departed member's access, sharing their personal channel
        read-only with the remaining team instead of deleting it.

        Kicks the member from the shared goal + orch channels (they're no
        longer part of the team space). Their personal channel is handled
        differently: invite every remaining member into it, kick the departed
        member out, then archive it — an archived Slack channel is natively
        read-only for everyone but stays visible to anyone with access, so no
        separate "read-only" flag is needed. The binding row is deliberately
        left untouched (not archived_at'd) so a rejoin finds and unarchives
        this same channel rather than ensure_user_goal_channel creating a new
        one — see ensure_user_goal_channel's existing-binding branch.
        """
        await _remove_member_from_goal_channel(goal_id, uid, pool)
        await _archive_and_share_personal_channel(goal_id, uid, pool)


async def _do_provision_goal(goal_id: str, integration_id: str, pool) -> str | None:
    """Create or refresh Slack channels for a goal. Idempotent.

    Branch A — existing GO binding found: refresh all member bindings
    Branch B — no existing GO binding: create full channel structure
    """
    from app.channels.slack.config import get_integration_credentials, save_goal_channel_config
    from app.channels.slack.lifecycle import setup_goal_channels

    goal = await pool.fetchrow(
        "SELECT name, go_thread_id FROM goals WHERE id = $1", goal_id,
    )
    if not goal:
        return "Goal not found"
    if not goal["go_thread_id"]:
        return "Goal thread not set up yet — save the goal and try again"

    creds = await get_integration_credentials(integration_id, pool)
    if not creds or not creds.get("bot_token"):
        return "No bot_token in Slack integration credentials"

    bot_token = creds["bot_token"]

    # Check for existing GO binding (idempotency gate)
    existing_go = await pool.fetchrow(
        """SELECT external_channel_id FROM platform_channel_bindings
           WHERE goal_id = $1 AND integration_id = $2
             AND actor_id IS NULL AND archived_at IS NULL
           LIMIT 1""",
        goal_id, integration_id,
    )

    if existing_go:
        # Branch A — update mode: refresh all member bindings
        _log.info(
            "Slack provision (update): goal=%s channel=%s — refreshing member bindings",
            goal_id, existing_go["external_channel_id"],
        )
        members = await pool.fetch(
            """SELECT u.uid FROM goal_members gm JOIN users u ON u.uid = gm.uid
               WHERE gm.goal_id = $1""",
            goal_id,
        )
        for m in members:
            try:
                await _invite_member_to_goal_channel(goal_id, m["uid"], pool)
            except Exception as exc:
                _log.warning(
                    "Slack provision (update): member invite failed goal=%s uid=%s: %s",
                    goal_id, m["uid"], exc,
                )
            try:
                await _do_provision_member(goal_id, m["uid"], pool)
            except Exception as exc:
                _log.warning(
                    "Slack provision (update): personal channel creation failed goal=%s uid=%s: %s",
                    goal_id, m["uid"], exc,
                )
        return None

    # Branch B — create mode: build full channel structure
    _log.info("Slack provision (create): starting for goal=%s", goal_id)
    try:
        channels = await setup_goal_channels(
            goal_id=goal_id,
            goal_name=goal["name"],
            integration_id=integration_id,
            bot_token=bot_token,
            pool=pool,
        )
    except Exception as exc:
        _log.error("Slack provision: channel creation failed goal=%s: %s", goal_id, exc, exc_info=True)
        return f"Slack channel creation failed: {exc}"

    # Persist channel IDs in goal_platform_integrations.config
    await save_goal_channel_config(
        goal_id=goal_id,
        integration_id=integration_id,
        goal_channel_id=channels["goal_channel_id"],
        orch_channel_id=channels["orch_channel_id"],
        goal_channel_name=channels["goal_channel_name"],
        orch_channel_name=channels["orch_channel_name"],
        pool=pool,
    )

    # Insert GO binding in platform_channel_bindings (actor_id IS NULL = goal channel)
    go_thread_id = str(goal["go_thread_id"])
    go_agent_id = await pool.fetchval("SELECT go_agent_id FROM goals WHERE id = $1", goal_id) or ""
    await pool.execute(
        """INSERT INTO platform_channel_bindings
               (goal_id, integration_id, thread_id, actor_id, agent_id, external_channel_id)
           VALUES ($1, $2, $3, NULL, $4, $5)
           ON CONFLICT (goal_id, integration_id, external_channel_id)
           DO UPDATE SET thread_id = $3, agent_id = $4, archived_at = NULL""",
        goal_id, integration_id, go_thread_id, go_agent_id,
        channels["goal_channel_id"],
    )
    _log.info(
        "Slack provision (create): GO binding saved goal=%s channel=%s thread=%s",
        goal_id, channels["goal_channel_id"], go_thread_id[:12],
    )

    # Invite all current goal members to the goal + orch channels.
    # Also attempt personal channel creation now for members who already have
    # an active SHCA thread — _do_provision_member skips gracefully when none exists,
    # so new members without an SHCA still get deferred to first story accept/start.
    members = await pool.fetch(
        """SELECT u.uid FROM goal_members gm JOIN users u ON u.uid = gm.uid
           WHERE gm.goal_id = $1""",
        goal_id,
    )
    member_errors = 0
    for m in members:
        try:
            await _invite_member_to_goal_channel(goal_id, m["uid"], pool)
        except Exception as exc:
            _log.warning(
                "Slack provision (create): member invite failed goal=%s uid=%s: %s",
                goal_id, m["uid"], exc,
            )
            member_errors += 1
        try:
            await _do_provision_member(goal_id, m["uid"], pool)
        except Exception as exc:
            _log.warning(
                "Slack provision (create): personal channel creation failed goal=%s uid=%s: %s",
                goal_id, m["uid"], exc,
            )

    _log.info(
        "Slack provision (create): done goal=%s members=%d errors=%d goal_ch=%s orch_ch=%s",
        goal_id, len(members), member_errors,
        channels["goal_channel_id"], channels["orch_channel_id"],
    )
    return None


async def _invite_member_to_goal_channel(goal_id: str, uid: str, pool) -> None:
    """Invite a member to the shared goal channel only.

    Called on member add. Personal channel creation is intentionally skipped
    here — it happens in _do_provision_member when the member's first story
    is accepted or started, matching the UI behaviour.

    Slack user ID resolution order:
    1. platform_user_map (confirmed mapping from prior Slack interaction)
    2. users.lookupByEmail — resolves by the member's AgentOS email address
    """
    from app.channels.slack.config import get_slack_config_for_goal
    from app.channels.slack.thread_map import get_slack_user_id_for_actor, map_slack_user

    cfg = await get_slack_config_for_goal(goal_id, pool)
    if not cfg or not cfg.get("bot_token") or not cfg.get("goal_channel_id"):
        _log.debug("Slack invite_to_goal_channel: no Slack config for goal=%s", goal_id)
        return

    user_row = await pool.fetchrow(
        "SELECT actor_id, email FROM users WHERE uid = $1", uid,
    )
    if not user_row or not user_row["actor_id"]:
        return

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    client = WebClient(token=cfg["bot_token"])

    # Try confirmed mapping first
    slack_user_id = await get_slack_user_id_for_actor(user_row["actor_id"], pool)

    # Fall back to email lookup if no mapping exists yet
    if not slack_user_id and user_row.get("email"):
        try:
            resp = client.users_lookupByEmail(email=user_row["email"])
            slack_user_id = resp["user"]["id"]
            _log.info(
                "Slack invite_to_goal_channel: resolved uid=%s via email lookup → %s",
                uid, slack_user_id,
            )
            # Persist the mapping now — this is Slack's own authoritative email lookup,
            # more reliable than the reactive exact-string match auto_map_by_email does
            # on first inbound message. Without this, new members have no mapping row
            # until (and unless) that reactive path happens to succeed on its own.
            await map_slack_user(
                slack_user_id, uid,
                slack_email=user_row["email"],
                pool=pool,
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack invite_to_goal_channel: email lookup failed for uid=%s (%s): %s",
                uid, user_row["email"], exc.response.get("error", exc),
            )

    if not slack_user_id:
        _log.info(
            "Slack invite_to_goal_channel: no Slack account found for uid=%s — skipping invite",
            uid,
        )
        return

    for channel_id in filter(None, [cfg.get("goal_channel_id"), cfg.get("orch_channel_id")]):
        try:
            client.conversations_invite(channel=channel_id, users=slack_user_id)
            _log.info("Slack invite_to_goal_channel: invited uid=%s to channel=%s", uid, channel_id)
        except SlackApiError as exc:
            error = exc.response.get("error", str(exc))
            if error == "already_in_channel":
                _log.debug("Slack invite_to_goal_channel: uid=%s already in channel=%s", uid, channel_id)
            else:
                _log.warning("Slack invite_to_goal_channel: failed uid=%s channel=%s: %s", uid, channel_id, error)


async def _remove_member_from_goal_channel(goal_id: str, uid: str, pool) -> None:
    """Kick a departed member from the shared goal + orch channels.

    Reverse of _invite_member_to_goal_channel, called on member removal. The
    member is no longer part of the team space, so unlike their personal
    channel (see _archive_and_share_personal_channel) there's no reason to
    keep them or anyone else able to see this after they've left.
    """
    from app.channels.slack.config import get_slack_config_for_goal
    from app.channels.slack.thread_map import get_slack_user_id_for_actor

    cfg = await get_slack_config_for_goal(goal_id, pool)
    if not cfg or not cfg.get("bot_token"):
        _log.debug("Slack remove_from_goal_channel: no Slack config for goal=%s", goal_id)
        return

    user_row = await pool.fetchrow(
        "SELECT actor_id FROM users WHERE uid = $1", uid,
    )
    if not user_row or not user_row["actor_id"]:
        return

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    client = WebClient(token=cfg["bot_token"])

    slack_user_id = await get_slack_user_id_for_actor(user_row["actor_id"], pool)
    if not slack_user_id:
        _log.info(
            "Slack remove_from_goal_channel: no Slack account found for uid=%s — skipping kick",
            uid,
        )
        return

    for channel_id in filter(None, [cfg.get("goal_channel_id"), cfg.get("orch_channel_id")]):
        try:
            client.conversations_kick(channel=channel_id, user=slack_user_id)
            _log.info("Slack remove_from_goal_channel: kicked uid=%s from channel=%s", uid, channel_id)
        except SlackApiError as exc:
            error = exc.response.get("error", str(exc))
            if error in ("not_in_channel", "cant_kick_self"):
                _log.debug("Slack remove_from_goal_channel: uid=%s already not in channel=%s", uid, channel_id)
            else:
                _log.warning("Slack remove_from_goal_channel: failed uid=%s channel=%s: %s", uid, channel_id, error)


async def _archive_and_share_personal_channel(goal_id: str, uid: str, pool) -> None:
    """Invite every remaining member into the departed member's personal
    channel, kick the departed member out, then archive it.

    Archiving is Slack's own read-only mechanism — every current member keeps
    read access to the history, and nobody (member or otherwise) can post,
    with no separate app-level flag needed on the Slack side. The binding row
    in platform_channel_bindings is deliberately left as-is (not archived_at'd)
    so a rejoin's ensure_user_goal_channel call finds and unarchives this same
    channel instead of provisioning a new one.
    """
    from app.channels.slack.config import get_slack_config_for_goal
    from app.channels.slack.thread_map import get_slack_user_id_for_actor

    cfg = await get_slack_config_for_goal(goal_id, pool)
    if not cfg or not cfg.get("bot_token"):
        _log.debug("Slack archive_and_share: no Slack config for goal=%s", goal_id)
        return

    user_row = await pool.fetchrow(
        "SELECT actor_id FROM users WHERE uid = $1", uid,
    )
    if not user_row or not user_row["actor_id"]:
        return

    binding = await pool.fetchrow(
        """SELECT external_channel_id FROM platform_channel_bindings
           WHERE goal_id = $1 AND integration_id = $2 AND actor_id = $3 AND archived_at IS NULL
           LIMIT 1""",
        goal_id, cfg["integration_id"], user_row["actor_id"],
    )
    if not binding:
        _log.debug("Slack archive_and_share: no personal channel binding for uid=%s goal=%s", uid, goal_id)
        return

    channel_id = binding["external_channel_id"]

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    client = WebClient(token=cfg["bot_token"])

    remaining = await pool.fetch(
        """SELECT u.actor_id FROM goal_members gm
             JOIN users u ON u.uid = gm.uid
            WHERE gm.goal_id = $1 AND u.actor_id IS NOT NULL""",
        goal_id,
    )
    for member in remaining:
        member_slack_id = await get_slack_user_id_for_actor(member["actor_id"], pool)
        if not member_slack_id:
            continue
        try:
            client.conversations_invite(channel=channel_id, users=member_slack_id)
        except SlackApiError as exc:
            error = exc.response.get("error", str(exc))
            if error != "already_in_channel":
                _log.warning(
                    "Slack archive_and_share: invite failed member=%s channel=%s: %s",
                    member_slack_id, channel_id, error,
                )

    departed_slack_id = await get_slack_user_id_for_actor(user_row["actor_id"], pool)
    if departed_slack_id:
        try:
            client.conversations_kick(channel=channel_id, user=departed_slack_id)
        except SlackApiError as exc:
            error = exc.response.get("error", str(exc))
            if error not in ("not_in_channel", "cant_kick_self"):
                _log.warning(
                    "Slack archive_and_share: kick failed uid=%s channel=%s: %s", uid, channel_id, error,
                )

    try:
        client.conversations_archive(channel=channel_id)
        _log.info(
            "Slack archive_and_share: archived channel=%s for departed uid=%s goal=%s "
            "(shared with %d remaining member(s))",
            channel_id, uid, goal_id, len(remaining),
        )
    except SlackApiError as exc:
        error = exc.response.get("error", str(exc))
        if error == "already_archived":
            _log.debug("Slack archive_and_share: channel=%s already archived", channel_id)
        else:
            _log.warning("Slack archive_and_share: archive failed channel=%s: %s", channel_id, error)


async def _do_provision_member(goal_id: str, uid: str, pool) -> None:
    """Create the personal user-goal Slack channel for a member.

    Called when a story is accepted or started — mirrors the UI behaviour where
    the assistant channel appears on first story import. Looks up the member's
    SHCA thread and calls ensure_user_goal_channel to create the binding.
    """
    from app.channels.slack.config import get_slack_config_for_goal
    from app.channels.slack.lifecycle import ensure_user_goal_channel
    from app.channels.slack.thread_map import get_slack_user_id_for_actor

    cfg = await get_slack_config_for_goal(goal_id, pool)
    if not cfg or not cfg.get("bot_token"):
        _log.debug("Slack provision_member: no Slack config for goal=%s", goal_id)
        return

    # Get user's actor_id and display name
    user_row = await pool.fetchrow(
        "SELECT actor_id, display_name, email FROM users WHERE uid = $1", uid,
    )
    if not user_row or not user_row["actor_id"]:
        _log.debug("Slack provision_member: no actor_id for uid=%s", uid)
        return

    actor_id = user_row["actor_id"]
    display_name = user_row["display_name"] or uid

    # Resolve Slack user ID for this member (may be None if not yet mapped)
    slack_user_id = await get_slack_user_id_for_actor(actor_id, pool)

    # Find the member's SHCA thread for this goal. Prefer a thread whose goal_id
    # matches exactly (per-goal isolation); fall back to any active SHCA thread
    # for threads created before per-goal isolation was introduced.
    shca_row = await pool.fetchrow(
        """SELECT t.id AS thread_id, a.id AS agent_id
             FROM threads t
             JOIN agents a ON a.id = t.agent_id
             JOIN agent_contexts ac ON ac.agent_id = a.id
            WHERE ac.goal_id = $1
              AND t.goal_id  = $1
              AND a.type = 'SHCA'
              AND a.status = 'active'
              AND a.owner_actor_id = $2
              AND (t.status IS NULL OR t.status IN ('active', 'open'))
              AND t.task_id IS NULL
            ORDER BY t.created_at DESC
            LIMIT 1""",
        goal_id, actor_id,
    )

    if not shca_row:
        # Fallback: legacy threads without goal_id set — accept any SHCA thread
        # registered in agent_contexts for this goal.
        shca_row = await pool.fetchrow(
            """SELECT t.id AS thread_id, a.id AS agent_id
                 FROM threads t
                 JOIN agents a ON a.id = t.agent_id
                 JOIN agent_contexts ac ON ac.agent_id = a.id
                WHERE ac.goal_id = $1
                  AND a.type = 'SHCA'
                  AND a.status = 'active'
                  AND a.owner_actor_id = $2
                  AND (t.status IS NULL OR t.status IN ('active', 'open'))
                  AND t.task_id IS NULL
                ORDER BY t.created_at DESC
                LIMIT 1""",
            goal_id, actor_id,
        )
        if shca_row:
            _log.warning(
                "Slack provision_member: no per-goal SHCA thread for uid=%s goal=%s "
                "— falling back to shared thread %s (upgrade path: accept story to create a per-goal thread)",
                uid, goal_id, str(shca_row["thread_id"])[:12],
            )

    if not shca_row:
        _log.info(
            "Slack provision_member: no SHCA thread found for uid=%s goal=%s — skipping personal channel",
            uid, goal_id,
        )
        return

    thread_id = str(shca_row["thread_id"])
    agent_id = str(shca_row["agent_id"])

    channel_id = await ensure_user_goal_channel(
        goal_id=goal_id,
        user_uid=uid,
        user_actor_id=actor_id,
        user_display_name=display_name,
        slack_user_id=slack_user_id or "",
        thread_id=thread_id,
        agent_id=agent_id,
        integration_id=cfg["integration_id"],
        bot_token=cfg["bot_token"],
        pool=pool,
    )

    if channel_id:
        _log.info(
            "Slack provision_member: provisioned uid=%s goal=%s channel=%s",
            uid, goal_id, channel_id,
        )
    else:
        _log.warning(
            "Slack provision_member: channel creation failed uid=%s goal=%s",
            uid, goal_id,
        )