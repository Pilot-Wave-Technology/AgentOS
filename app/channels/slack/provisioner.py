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

from app.channels.slack.client import make_async_client
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

        It used to also create a private (member x goal) channel. That
        tier is gone -- see the note where `_do_provision_member` was.
        A member now gets the goal channel here and a story channel when
        there is a story, which is every channel they ever read.
        """
        await _invite_member_to_goal_channel(goal_id, uid, pool)

    async def deprovision_member(self, goal_id: str, uid: str, pool) -> None:
        """Remove a departed member's access, sharing their personal channel
        read-only with the remaining team instead of deleting it.

        Kicks the member from the shared goal + orch channels (they're no
        longer part of the team space). Their personal channel is handled
        differently: invite every remaining member into it, kick the departed
        member out, then archive it — an archived Slack channel is natively
        read-only for everyone but stays visible to anyone with access, so no
        separate "read-only" flag is needed. The binding row is deliberately
        left untouched (not archived_at'd).

        THAT LAST PART IS NOW ONLY FOR THE RECORD. It was there so a
        rejoin would find and unarchive the same channel; the path that
        did the unarchiving went with the (member x goal) tier on
        2026-10-19. This still runs because channels created before the
        removal are still in the table and a departing member's should
        still go read-only — but nothing will bring one back.
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
            # No personal channel to create any more: the (member x goal)
            # tier was removed on 2026-10-19. The invite above is now the
            # whole of what a member gets here; their story channels come
            # from `thread_channels.ensure_channel_for_thread`.
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
               (goal_id, integration_id, thread_id, actor_id, agent_id,
                external_channel_id, channel_name)
           VALUES ($1, $2, $3, NULL, $4, $5, $6)
           ON CONFLICT (goal_id, integration_id, external_channel_id)
           DO UPDATE SET thread_id = $3, agent_id = $4, archived_at = NULL,
                         channel_name = COALESCE(EXCLUDED.channel_name,
                                                 platform_channel_bindings.channel_name)""",
        goal_id, integration_id, go_thread_id, go_agent_id,
        channels["goal_channel_id"], channels.get("goal_channel_name") or None,
    )
    _log.info(
        "Slack provision (create): GO binding saved goal=%s channel=%s thread=%s",
        goal_id, channels["goal_channel_id"], go_thread_id[:12],
    )

    # Invite all current goal members to the goal + orch channels.
    # That is all this does now: the private (member x goal) channel it
    # also used to create was removed on 2026-10-19.
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
        # Same as the update path above: no personal channel to create.

    _log.info(
        "Slack provision (create): done goal=%s members=%d errors=%d goal_ch=%s orch_ch=%s",
        goal_id, len(members), member_errors,
        channels["goal_channel_id"], channels["orch_channel_id"],
    )
    return None


async def _invite_member_to_goal_channel(goal_id: str, uid: str, pool) -> None:
    """Invite a member to the shared goal channel only.

    Called on member add. There is no personal channel to create: that
    tier was removed on 2026-10-19 and a member's other channels are the
    story ones, opened per story thread.

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

    from slack_sdk.errors import SlackApiError
    client = make_async_client(cfg["bot_token"])

    # Try confirmed mapping first
    slack_user_id = await get_slack_user_id_for_actor(user_row["actor_id"], pool)

    # Fall back to email lookup if no mapping exists yet
    if not slack_user_id and user_row.get("email"):
        try:
            resp = await client.users_lookupByEmail(email=user_row["email"])
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
            await client.conversations_invite(channel=channel_id, users=slack_user_id)
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

    from slack_sdk.errors import SlackApiError
    client = make_async_client(cfg["bot_token"])

    slack_user_id = await get_slack_user_id_for_actor(user_row["actor_id"], pool)
    if not slack_user_id:
        _log.info(
            "Slack remove_from_goal_channel: no Slack account found for uid=%s — skipping kick",
            uid,
        )
        return

    for channel_id in filter(None, [cfg.get("goal_channel_id"), cfg.get("orch_channel_id")]):
        try:
            await client.conversations_kick(channel=channel_id, user=slack_user_id)
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
    in platform_channel_bindings is deliberately left as-is (not archived_at'd).

    LEGACY ONLY SINCE 2026-10-19. The (member x goal) tier was removed,
    so no new personal channel is ever created and nothing unarchives an
    old one. This keeps running for the channels that already exist: a
    departing member's should still become read-only. Once those are
    gone from the table, so can this.
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

    from slack_sdk.errors import SlackApiError
    client = make_async_client(cfg["bot_token"])

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
            await client.conversations_invite(channel=channel_id, users=member_slack_id)
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
            await client.conversations_kick(channel=channel_id, user=departed_slack_id)
        except SlackApiError as exc:
            error = exc.response.get("error", str(exc))
            if error not in ("not_in_channel", "cant_kick_self"):
                _log.warning(
                    "Slack archive_and_share: kick failed uid=%s channel=%s: %s", uid, channel_id, error,
                )

    try:
        await client.conversations_archive(channel=channel_id)
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


# `_do_provision_member` WAS HERE AND IS GONE (2026-10-19).
#
# It created the private (member x goal) Slack channel on story accept
# or start. That tier predated story channels and had stopped carrying
# anything a person reads -- the conversation, the cards and the agent's
# replies all land in the story's own channel.
#
# It was also where `<goal>-<name>-<env>-2` came from. It resolved the
# member's SHCA thread and handed it to `ensure_user_goal_channel`,
# whose lookup matched only an UNARCHIVED binding -- so after a binding
# was archived it made a FRESH Slack channel beside the one that still
# existed, and Slack numbered it.
#
# Members still get the goal channel (`_invite_member_to_goal_channel`,
# above) and a story channel per story thread
# (`thread_channels.ensure_channel_for_thread`). Nothing else was lost.
