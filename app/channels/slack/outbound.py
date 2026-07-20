"""Slack outbound relay — post agent responses and system messages to Slack channels.

Uses platform_channel_bindings (generic channel system) for thread → Slack channel
resolution. Credentials come from platform_integrations. No imports from app.slack.*.

Entry points registered in channels/registry.py:
  relay_agent_message   → _OUTBOUND_RELAYS["slack"]
  post_to_goal_page     → _GOAL_CHANNEL_RELAYS["slack"]
  post_to_thread_page   → _THREAD_CHANNEL_RELAYS["slack"]

Echo prevention: bot posts are filtered in inbound.py via bot_user_id check, so
posting here does not trigger another inbound event.
"""
import logging

_log = logging.getLogger(__name__)

_TEXT_LIMIT = 39000  # Slack chat.postMessage text ceiling (~40k chars)


def _truncate_text(content: str) -> str:
    if len(content) <= _TEXT_LIMIT:
        return content
    return content[:_TEXT_LIMIT] + "\n…_[truncated]_"


async def _get_bot_token(integration_id: str, pool) -> str:
    """Return the bot_token for a platform_integrations row."""
    from app.channels.slack.config import get_integration_credentials
    creds = await get_integration_credentials(integration_id, pool)
    return (creds or {}).get("bot_token", "")


async def _post_message(
    bot_token: str,
    channel: str,
    text: str,
    username: str = "System",
    blocks: list | None = None,
) -> str:
    """Post a single message to a Slack channel. Raises SlackApiError on failure.

    Returns the Slack message ts (timestamp) so callers can stamp external_message_id.
    """
    from slack_sdk import WebClient
    client = WebClient(token=bot_token)
    resp = client.chat_postMessage(
        channel=channel,
        text=text,
        username=username,
        blocks=blocks or None,
    )
    return resp.get("ts", "")


# ── Registered outbound relay ─────────────────────────────────────────────────

async def relay_agent_message(
    goal_id: str,
    thread_id: str,
    content: str,
    msg_id=None,
) -> None:
    """Post an agent's final reply to the Slack channel mapped to this thread.

    Registered as _OUTBOUND_RELAYS["slack"]. Called by relay_all_outbound()
    from chat.py / goal_thread.py after every agent message. Fire-and-forget.
    """
    if not content or not content.strip():
        return

    try:
        from slack_sdk.errors import SlackApiError
        from app.channels.base import get_binding_by_thread
        from app.database import get_pool

        pool = get_pool()
        binding = await get_binding_by_thread("slack", thread_id, goal_id)
        if not binding:
            _log.debug(
                "Slack relay: no binding for thread=%s goal=%s",
                thread_id[:12] if thread_id else "", goal_id,
            )
            return

        external_channel_id = binding["external_channel_id"]
        integration_id = binding["integration_id"]
        bot_token = await _get_bot_token(str(integration_id), pool)
        if not bot_token:
            _log.warning("Slack relay: no bot_token for integration=%s", integration_id)
            return

        actor_id = binding.get("actor_id")
        if actor_id:
            display = await pool.fetchval(
                "SELECT display_name FROM users WHERE actor_id = $1", actor_id,
            )
            username = f"{display}'s Assistant" if display else "Assistant"
        else:
            username = "Goal Orchestrator"

        text = _truncate_text(content)
        try:
            slack_ts = await _post_message(bot_token, external_channel_id, text, username=username)
            _log.info(
                "Slack relay OK: goal=%s thread=%s channel=%s len=%d ts=%s",
                goal_id, thread_id[:12], external_channel_id, len(text), slack_ts,
            )
            # Stamp the Slack ts so the inbound handler can skip the echo even if
            # Slack's bot_id filter misfires (e.g. username-override edge cases).
            if slack_ts and msg_id:
                await pool.execute(
                    "UPDATE messages SET external_message_id = $1 WHERE id = $2",
                    slack_ts, msg_id,
                )
        except SlackApiError as exc:
            _log.warning(
                "Slack relay failed: goal=%s thread=%s channel=%s: %s",
                goal_id, thread_id[:12], external_channel_id,
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack relay error: goal=%s thread=%s: %s",
            goal_id, thread_id[:12] if thread_id else "", exc, exc_info=True,
        )


async def relay_human_message(
    goal_id: str,
    thread_id: str,
    content: str,
    user_uid: str,
    msg_id: str = "",
) -> None:
    """Mirror a UI-originated human message to the Slack channel for this thread.

    Posts as the user's display name so Slack readers see who typed. Stamps the
    returned Slack ts on messages.external_message_id so the inbound handler can
    detect and skip the echo — prevents the message from appearing twice in the UI.
    Fire-and-forget.
    """
    if not content or not content.strip():
        return

    try:
        from slack_sdk.errors import SlackApiError
        from app.channels.base import get_binding_by_thread
        from app.database import get_pool

        pool = get_pool()
        binding = await get_binding_by_thread("slack", thread_id, goal_id)
        if not binding:
            return

        external_channel_id = binding["external_channel_id"]
        bot_token = await _get_bot_token(str(binding["integration_id"]), pool)
        if not bot_token:
            return

        display = await pool.fetchval("SELECT display_name FROM users WHERE uid = $1", user_uid)
        username = display or "Team member"
        text = _truncate_text(content)

        try:
            slack_ts = await _post_message(bot_token, external_channel_id, text, username=username)
            _log.info(
                "Slack human relay OK: goal=%s thread=%s channel=%s user=%s ts=%s",
                goal_id, thread_id[:12], external_channel_id, user_uid, slack_ts,
            )
            # Stamp the returned ts so the inbound handler can skip the echo.
            if slack_ts and msg_id:
                await pool.execute(
                    "UPDATE messages SET external_message_id = $1 WHERE id = $2",
                    slack_ts, msg_id,
                )
        except SlackApiError as exc:
            _log.warning(
                "Slack human relay failed: goal=%s thread=%s: %s",
                goal_id, thread_id[:12],
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack human relay error: goal=%s thread=%s: %s",
            goal_id, thread_id[:12] if thread_id else "", exc, exc_info=True,
        )


# ── Goal-channel relay ─────────────────────────────────────────────────────────

async def post_to_goal_page(
    goal_id: str,
    content: str,
    username: str = "System",
) -> None:
    """Post a message to the goal's main Slack channel.

    Registered as _GOAL_CHANNEL_RELAYS["slack"]. Used for lifecycle events the
    whole team should see (story created by GO, decision recorded, etc.).
    Fire-and-forget.
    """
    if not content or not content.strip():
        return

    try:
        from slack_sdk.errors import SlackApiError
        from app.channels.slack.config import get_slack_config_for_goal
        from app.database import get_pool

        pool = get_pool()
        cfg = await get_slack_config_for_goal(goal_id, pool)
        if not cfg or not cfg.get("goal_channel_id") or not cfg.get("bot_token"):
            return

        text = _truncate_text(content)
        try:
            await _post_message(cfg["bot_token"], cfg["goal_channel_id"], text, username=username)
            _log.info(
                "Slack goal-channel relay OK: goal=%s channel=%s username=%r len=%d",
                goal_id, cfg["goal_channel_id"], username, len(text),
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack goal-channel relay failed: goal=%s: %s",
                goal_id, exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning("Slack goal-channel relay error: goal=%s: %s", goal_id, exc, exc_info=True)


# ── Thread-channel relay ───────────────────────────────────────────────────────

async def post_to_thread_page(
    goal_id: str,
    thread_id: str,
    content: str,
    username: str = "System",
) -> None:
    """Post a message to the Slack channel mapped to a specific thread.

    Registered as _THREAD_CHANNEL_RELAYS["slack"]. Used for thread-scoped system
    messages (GO reply to SHCA, task dispatched, etc.). Fire-and-forget.
    """
    if not content or not content.strip():
        return

    try:
        from slack_sdk.errors import SlackApiError
        from app.channels.base import get_binding_by_thread
        from app.database import get_pool

        pool = get_pool()
        binding = await get_binding_by_thread("slack", thread_id, goal_id)
        if not binding:
            _log.debug(
                "Slack thread relay: no binding for thread=%s goal=%s",
                thread_id[:12] if thread_id else "", goal_id,
            )
            return

        external_channel_id = binding["external_channel_id"]
        bot_token = await _get_bot_token(str(binding["integration_id"]), pool)
        if not bot_token:
            return

        text = _truncate_text(content)
        try:
            await _post_message(bot_token, external_channel_id, text, username=username)
            _log.info(
                "Slack thread relay OK: goal=%s thread=%s channel=%s username=%r",
                goal_id, thread_id[:12], external_channel_id, username,
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack thread relay failed: goal=%s thread=%s: %s",
                goal_id, thread_id[:12],
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack thread relay error: goal=%s thread=%s: %s",
            goal_id, thread_id[:12] if thread_id else "", exc, exc_info=True,
        )


# ── Orchestrator channel relay ─────────────────────────────────────────────────

async def post_to_orchestrator_channel(
    goal_id: str,
    content: str,
    username: str = "System",
) -> None:
    """Post to the goal's Slack orchestrator channel (read-only coordination log)."""
    if not content or not content.strip():
        return

    try:
        from slack_sdk.errors import SlackApiError
        from app.channels.slack.config import get_slack_config_for_goal
        from app.database import get_pool

        pool = get_pool()
        cfg = await get_slack_config_for_goal(goal_id, pool)
        if not cfg or not cfg.get("orch_channel_id") or not cfg.get("bot_token"):
            return

        text = _truncate_text(content)
        try:
            await _post_message(cfg["bot_token"], cfg["orch_channel_id"], text, username=username)
            _log.info(
                "Slack orch relay OK: goal=%s channel=%s username=%r len=%d",
                goal_id, cfg["orch_channel_id"], username, len(text),
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack orch relay failed: goal=%s: %s",
                goal_id, exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning("Slack orch relay error: goal=%s: %s", goal_id, exc, exc_info=True)


async def relay_orchestrator_message(goal_id: str, content: str) -> None:
    """Post an orchestrator-scoped message to both goal channel and orch channel.

    Called from lifecycle.py for orchestrator.message events, mirroring the
    Notion side's relay_orchestrator_message signature.
    """
    await post_to_goal_page(goal_id, content, username="Goal Orchestrator")
    await post_to_orchestrator_channel(goal_id, content, username="Goal Orchestrator")


# ── Card relay ────────────────────────────────────────────────────────────────

async def relay_card(
    goal_id: str,
    thread_id: str,
    card: dict,
    fallback_text: str,
) -> None:
    """Post an agent card as Block Kit to the Slack channel mapped to this thread.

    Falls back to plain text if no block renderer exists for the card type.
    """
    if not card:
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from app.channels.base import get_binding_by_thread
        from app.channels.slack.blocks import render_card_as_blocks
        from app.database import get_pool

        pool = get_pool()
        binding = await get_binding_by_thread("slack", thread_id, goal_id)
        if not binding:
            _log.debug("Slack card relay: no binding for thread=%s", thread_id[:12] if thread_id else "")
            return

        external_channel_id = binding["external_channel_id"]
        bot_token = await _get_bot_token(str(binding["integration_id"]), pool)
        if not bot_token:
            return

        blocks = render_card_as_blocks(card)
        if not blocks:
            _log.debug(
                "Slack card relay: no block renderer for card_type=%s — falling back to text",
                card.get("card_type", ""),
            )

        username = card.get("agent_name") or "Agent"
        text = fallback_text[:2500] + "…" if len(fallback_text) > 2500 else fallback_text

        client = WebClient(token=bot_token)
        try:
            client.chat_postMessage(
                channel=external_channel_id,
                text=text,
                blocks=blocks or None,
                username=username,
            )
            _log.info(
                "Slack card relay OK: goal=%s thread=%s channel=%s type=%s",
                goal_id, thread_id[:12] if thread_id else "", external_channel_id,
                card.get("card_type", "?"),
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack card relay failed: thread=%s type=%s: %s",
                thread_id[:12] if thread_id else "", card.get("card_type", "?"),
                exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack card relay error: thread=%s: %s",
            thread_id[:12] if thread_id else "", exc, exc_info=True,
        )


# ── Operator-mode plan ready card ─────────────────────────────────────────────

async def relay_plan_ready(
    goal_id: str,
    thread_id: str,
    story_id: str,
    story_title: str,
    plan_path: str = "",
) -> None:
    """Post a 'Plan ready' Block Kit card to the Slack channel for this thread.

    Called from internal_shca.validate-plan when SHCA writes a plan in operator
    mode. Gives the human an Approve Plan / Request Changes button pair in Slack
    without requiring a page refresh in the web UI.
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from app.channels.base import get_binding_by_thread
        from app.channels.slack.blocks import plan_ready_blocks
        from app.database import get_pool

        pool = get_pool()
        binding = await get_binding_by_thread("slack", thread_id, goal_id)
        if not binding:
            _log.debug(
                "relay_plan_ready: no Slack binding for goal=%s thread=%s",
                goal_id, thread_id[:12] if thread_id else "",
            )
            return

        external_channel_id = binding["external_channel_id"]
        bot_token = await _get_bot_token(str(binding["integration_id"]), pool)
        if not bot_token:
            return

        blocks = plan_ready_blocks(story_id, story_title, plan_path)
        fallback = f"Plan ready for story '{story_title}'. Approve or request changes in the control panel."

        client = WebClient(token=bot_token)
        try:
            client.chat_postMessage(
                channel=external_channel_id,
                text=fallback,
                blocks=blocks,
                username="Plan",
            )
            _log.info(
                "relay_plan_ready OK: goal=%s story=%s channel=%s",
                goal_id, story_id[:8], external_channel_id,
            )
        except SlackApiError as exc:
            _log.warning(
                "relay_plan_ready Slack error: story=%s: %s",
                story_id[:8], exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning("relay_plan_ready error: story=%s: %s", story_id[:8], exc, exc_info=True)


# ── Phase task progress relay ─────────────────────────────────────────────────

async def relay_phase_task_progress(
    goal_id: str,
    thread_id: str,
    story_id: str,
    task_id: str,
    phase: str,
    sentinel: str,
    text: str,
) -> None:
    """Post a compact task-progress notification to the Slack channel bound to this thread.

    Includes a Pause button so operators can halt story execution from Slack
    without opening the control panel.
    """
    if not thread_id:
        return
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from app.channels.base import get_binding_by_thread
        from app.database import get_pool

        pool = get_pool()
        binding = await get_binding_by_thread("slack", thread_id, goal_id)
        if not binding:
            return

        external_channel_id = binding["external_channel_id"]
        bot_token = await _get_bot_token(str(binding["integration_id"]), pool)
        if not bot_token:
            return

        sentinel_label = (sentinel or "").upper() or "OUTPUT"
        short_id = task_id[:8] if task_id else "?"
        header_line = f"`{short_id}` · {phase} · {sentinel_label}"
        summary = (text or "").strip()
        if len(summary) > 300:
            summary = summary[:297] + "…"

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header_line},
            },
        ]
        if summary:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": summary}],
            })
        if story_id:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⏸ Pause", "emoji": True},
                        "style": "danger",
                        "action_id": f"ctrl_pause_{story_id}",
                        "value": story_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🔄 Replan", "emoji": True},
                        "action_id": f"ctrl_replan_{story_id}",
                        "value": story_id,
                    },
                ],
            })

        client = WebClient(token=bot_token)
        try:
            client.chat_postMessage(
                channel=external_channel_id,
                text=header_line,
                blocks=blocks,
                username="Task Controller",
            )
            _log.info(
                "Slack task progress relay OK: goal=%s thread=%s task=%s sentinel=%s",
                goal_id, thread_id[:12], short_id, sentinel_label,
            )
        except SlackApiError as exc:
            _log.warning(
                "Slack task progress relay failed: thread=%s task=%s: %s",
                thread_id[:12], short_id, exc.response.get("error", str(exc)),
            )
    except Exception as exc:
        _log.warning(
            "Slack task progress relay error: thread=%s: %s",
            thread_id[:12] if thread_id else "", exc, exc_info=True,
        )