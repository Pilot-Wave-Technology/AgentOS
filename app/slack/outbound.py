"""DEPRECATED — superseded by app/channels/slack/outbound.py.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Slack outbound relay — post agent responses to the right Slack channel.

Entry point: `relay_agent_message(goal_id, thread_id, content)`. Called from
openclaw.py after every agent `chat:final` event. No-op if:
  - the goal has no Slack config
  - the thread isn't mapped to any Slack channel (goal thread or story channel)

Echo prevention: bot-posted messages are filtered in router.py via `bot_id`
and `bot_user_id` checks, so posting here does not trigger another inbound.
"""
import logging

_log = logging.getLogger(__name__)


async def relay_agent_message(goal_id: str, thread_id: str, content: str) -> None:
    """Post an agent's final reply to the Slack channel mapped to this thread.

    Fire-and-forget — never raises. If the Slack module isn't available,
    or the thread isn't mapped, this is a no-op.
    """
    if not content or not content.strip():
        _log.debug("Relay skipped (empty): goal=%s thread=%s", goal_id, thread_id[:12])
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        from app.database import get_pool
        from app.slack.config import get_goal_slack_config
        from app.slack import story_channels as story_ch
        from app.slack import user_goal_channels as ugc
    except ImportError:
        return

    try:
        config = await get_goal_slack_config(goal_id)
        if not config or not config.get("enabled"):
            _log.debug("Relay skipped (no slack config): goal=%s", goal_id)
            return

        bot_token = config.get("bot_token", "")
        if not bot_token:
            _log.warning("Relay skipped (no bot_token): goal=%s", goal_id)
            return

        pool = get_pool()

        goal = await pool.fetchrow(
            "SELECT go_thread_id FROM goals WHERE id = $1", goal_id,
        )
        target_channel = ""
        username = "Assistant"
        channel_kind = ""

        if goal and goal.get("go_thread_id") == thread_id:
            target_channel = config["goal_channel_id"]
            username = "Goal Orchestrator"
            channel_kind = "goal"
        else:
            # Post-pivot routing: per-(user × goal) channel is canonical;
            # legacy per-story is the fallback for old data.
            mapping = await ugc.get_by_thread_id(thread_id)
            if mapping and not mapping.get("archived_at"):
                target_channel = mapping["slack_channel_id"]
                display = await pool.fetchval(
                    "SELECT display_name FROM users WHERE uid = $1",
                    mapping["user_uid"],
                )
                username = f"{display}'s Assistant" if display else "Assistant"
                channel_kind = "user_goal"
            else:
                legacy = await story_ch.get_by_thread_id(thread_id)
                if legacy and not legacy.get("archived_at"):
                    target_channel = legacy["slack_channel_id"]
                    display = await pool.fetchval(
                        "SELECT display_name FROM users WHERE uid = $1",
                        legacy["user_uid"],
                    )
                    username = f"{display}'s Assistant" if display else "Assistant"
                    channel_kind = "story_legacy"
                else:
                    _log.info(
                        "Relay skipped (no channel mapping): goal=%s thread=%s "
                        "— no row in slack_user_goal_channels OR slack_story_channels "
                        "for this thread_id",
                        goal_id, thread_id[:12],
                    )
                    return

        if not target_channel:
            _log.warning("Relay skipped (no target channel resolved): goal=%s thread=%s",
                         goal_id, thread_id[:12])
            return

        # Slack's `text` has a ~40k-char ceiling; truncate defensively.
        text = content if len(content) <= 39000 else content[:39000] + "\n…_[truncated]_"

        client = WebClient(token=bot_token)
        try:
            client.chat_postMessage(
                channel=target_channel,
                text=text,
                username=username,
            )
            _log.info(
                "Slack relay OK: goal=%s thread=%s kind=%s channel=%s len=%d",
                goal_id, thread_id[:12], channel_kind, target_channel, len(text),
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack relay failed (goal=%s thread=%s channel=%s kind=%s): %s",
                goal_id, thread_id[:12], target_channel, channel_kind,
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack relay error (goal=%s thread=%s): %s",
            goal_id, thread_id[:12], exc, exc_info=True,
        )


async def post_to_goal_channel(
    goal_id: str, content: str, username: str = "System",
) -> None:
    """Post a message to the goal's main Slack channel. Fire-and-forget.

    Used for lifecycle events the whole team should see (story created by
    the GO, decision recorded, etc.).
    """
    if not content or not content.strip():
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        from app.slack.config import get_goal_slack_config
    except ImportError:
        return

    try:
        config = await get_goal_slack_config(goal_id)
        if not config or not config.get("enabled"):
            return

        goal_channel_id = config.get("goal_channel_id", "")
        bot_token = config.get("bot_token", "")
        if not goal_channel_id or not bot_token:
            return

        text = content if len(content) <= 39000 else content[:39000] + "\n…_[truncated]_"

        client = WebClient(token=bot_token)
        try:
            client.chat_postMessage(
                channel=goal_channel_id,
                text=text,
                username=username,
            )
            _log.info(
                "Slack goal relay OK: goal=%s channel=%s username=%r len=%d",
                goal_id, goal_channel_id, username, len(text),
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack goal relay failed (goal=%s): %s",
                goal_id, exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack goal relay error (goal=%s): %s", goal_id, exc, exc_info=True,
        )


async def post_to_orchestrator_channel(
    goal_id: str, content: str, username: str = "System",
) -> None:
    """Post a raw message to the goal's Slack orchestrator channel.

    Used by the SHCA→GO and GO→SHCA orchestrator endpoints. Fire-and-forget.
    """
    if not content or not content.strip():
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        from app.slack.config import get_goal_slack_config
    except ImportError:
        return

    try:
        config = await get_goal_slack_config(goal_id)
        if not config or not config.get("enabled"):
            return

        orch_channel_id = config.get("orch_channel_id", "")
        bot_token = config.get("bot_token", "")
        if not orch_channel_id or not bot_token:
            _log.debug(
                "Orch relay skipped (missing orch channel or token): goal=%s",
                goal_id,
            )
            return

        text = content if len(content) <= 39000 else content[:39000] + "\n…_[truncated]_"

        client = WebClient(token=bot_token)
        try:
            client.chat_postMessage(
                channel=orch_channel_id,
                text=text,
                username=username,
            )
            _log.info(
                "Slack orch relay OK: goal=%s channel=%s username=%r len=%d",
                goal_id, orch_channel_id, username, len(text),
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack orch relay failed (goal=%s channel=%s): %s",
                goal_id, orch_channel_id,
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack orch relay error (goal=%s): %s", goal_id, exc, exc_info=True,
        )


async def relay_card_to_slack(
    goal_id: str,
    thread_id: str,
    card: dict,
    fallback_text: str,
) -> None:
    """Post an agent card to the Slack story channel mapped to this thread.

    Renders the card as Block Kit (interactive buttons where applicable).
    `fallback_text` is used as the plain-text fallback that notifications
    and accessibility readers show.
    """
    if not card:
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        from app.slack.config import get_goal_slack_config
        from app.slack import story_channels as story_ch
        from app.slack import user_goal_channels as ugc
        from app.slack.blocks import render_card_as_blocks
    except ImportError:
        return

    try:
        config = await get_goal_slack_config(goal_id)
        if not config or not config.get("enabled"):
            return

        # Post-pivot routing: prefer slack_user_goal_channels.
        mapping = await ugc.get_by_thread_id(thread_id)
        if not mapping or mapping.get("archived_at"):
            mapping = await story_ch.get_by_thread_id(thread_id)
        if not mapping or mapping.get("archived_at"):
            _log.debug(
                "Card relay skipped (no channel mapping for thread %s)",
                thread_id[:12],
            )
            return

        blocks = render_card_as_blocks(card)
        if not blocks:
            _log.debug(
                "Card relay: no block renderer for card_type=%s — falling back to text",
                card.get("card_type", ""),
            )

        username = card.get("agent_name") or "Agent"
        text = fallback_text if len(fallback_text) <= 2500 else fallback_text[:2500] + "…"

        client = WebClient(token=config["bot_token"])
        try:
            client.chat_postMessage(
                channel=mapping["slack_channel_id"],
                text=text,
                blocks=blocks or None,
                username=username,
            )
            _log.info(
                "Slack card relay OK: goal=%s thread=%s channel=%s type=%s",
                goal_id, thread_id[:12], mapping["slack_channel_id"],
                card.get("card_type", "?"),
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack card relay failed (thread=%s type=%s): %s",
                thread_id[:12], card.get("card_type", "?"),
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack card relay error (thread=%s): %s",
            thread_id[:12], exc, exc_info=True,
        )


async def post_to_story_channel(
    goal_id: str, thread_id: str, content: str, username: str = "Goal Orchestrator",
) -> None:
    """Post a message to the Slack story channel mapped to this thread.

    Used when the GO replies to an assistant via reply_assistant — the
    reply is injected into the SHCA thread and should also appear in the
    Slack story channel so the user sees the GO's message before the SHCA
    continues.
    """
    if not content or not content.strip():
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        from app.slack.config import get_goal_slack_config
        from app.slack import story_channels as story_ch
        from app.slack import user_goal_channels as ugc
    except ImportError:
        return

    try:
        config = await get_goal_slack_config(goal_id)
        if not config or not config.get("enabled"):
            return

        # Post-pivot routing: prefer slack_user_goal_channels.
        mapping = await ugc.get_by_thread_id(thread_id)
        if not mapping or mapping.get("archived_at"):
            mapping = await story_ch.get_by_thread_id(thread_id)
        if not mapping or mapping.get("archived_at"):
            _log.debug(
                "post_to_story_channel relay skipped (no active mapping): thread=%s",
                thread_id[:12],
            )
            return

        text = content if len(content) <= 39000 else content[:39000] + "\n…_[truncated]_"

        client = WebClient(token=config["bot_token"])
        try:
            client.chat_postMessage(
                channel=mapping["slack_channel_id"],
                text=text,
                username=username,
            )
            _log.info(
                "Slack story relay OK: goal=%s thread=%s channel=%s username=%r",
                goal_id, thread_id[:12], mapping["slack_channel_id"], username,
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack story relay failed (thread=%s): %s",
                thread_id[:12], exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack story relay error (thread=%s): %s",
            thread_id[:12], exc, exc_info=True,
        )


async def relay_human_message(
    goal_id: str, thread_id: str, content: str, user_uid: str,
) -> None:
    """Mirror a UI-originated human message to the Slack channel mapped to
    this thread, so Slack viewers see what the user typed in the control panel.

    Channel routing matches relay_agent_message. Posts as the user's display
    name (not the bot label) so Slack readers can tell who spoke. Fire-and-
    forget; never raises. No-op when Slack isn't enabled for the goal or the
    thread isn't mapped to any Slack channel.

    Echo prevention: bot posts are filtered by router.py via bot_id /
    bot_user_id, so this won't trigger another inbound.
    """
    if not content or not content.strip():
        return

    # Strip the SHCA state-context block (if any) so Slack viewers see
    # only the human's original text, not the appended snapshot. The
    # block is for SHCA's LLM only.
    try:
        from app.services.story_state import strip_state_context
        content = strip_state_context(content)
    except Exception:
        pass

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        from app.database import get_pool
        from app.slack.config import get_goal_slack_config
        from app.slack import story_channels as story_ch
        from app.slack import user_goal_channels as ugc
    except ImportError:
        return

    try:
        config = await get_goal_slack_config(goal_id)
        if not config or not config.get("enabled"):
            return

        bot_token = config.get("bot_token", "")
        if not bot_token:
            return

        pool = get_pool()

        goal = await pool.fetchrow(
            "SELECT go_thread_id FROM goals WHERE id = $1", goal_id,
        )
        target_channel = ""

        if goal and goal.get("go_thread_id") == thread_id:
            target_channel = config["goal_channel_id"]
        else:
            mapping = await ugc.get_by_thread_id(thread_id)
            if mapping and not mapping.get("archived_at"):
                target_channel = mapping["slack_channel_id"]
            else:
                legacy = await story_ch.get_by_thread_id(thread_id)
                if legacy and not legacy.get("archived_at"):
                    target_channel = legacy["slack_channel_id"]
                else:
                    _log.info(
                        "Human relay skipped (no channel mapping): goal=%s thread=%s",
                        goal_id, thread_id[:12],
                    )
                    return

        if not target_channel:
            return

        display = await pool.fetchval(
            "SELECT display_name FROM users WHERE uid = $1", user_uid,
        )
        username = display or "Team member"

        text = content if len(content) <= 39000 else content[:39000] + "\n…_[truncated]_"

        client = WebClient(token=bot_token)
        try:
            client.chat_postMessage(
                channel=target_channel,
                text=text,
                username=username,
            )
            _log.info(
                "Slack human relay OK: goal=%s thread=%s channel=%s user=%s",
                goal_id, thread_id[:12], target_channel, user_uid,
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack human relay failed (goal=%s thread=%s): %s",
                goal_id, thread_id[:12],
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack human relay error (goal=%s thread=%s): %s",
            goal_id, thread_id[:12], exc, exc_info=True,
        )
