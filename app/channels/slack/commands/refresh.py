"""/goal refresh — reset the gateway session for the agent owning this channel.

Only reachable as a /goal subcommand (see goal.py) — there is no standalone
/refresh slash command registered with the Slack App.
"""
import asyncio
import logging

from app.database import get_pool
from app.openclaw import make_goal_session_key
from app.channels.slack.commands._helpers import (
    resolve_goal_config, resolve_user, run_followup, post_followup,
)

_log = logging.getLogger(__name__)


async def handle(
    slack_user_id: str, channel_id: str, team_id: str,
    response_url: str = "",
    command: str = "",
) -> str:
    """Resolves the agent owning this channel and resets its gateway session.

    SHCA user channel (actor_id IS NOT NULL) → refresh that user's SHCA.
    GO main channel or orchestrator channel → refresh the GO.
    """
    if not command:
        from app.channels.slack.commands.registry import _cmd_prefix
        command = _cmd_prefix("goal")
    pool = get_pool()
    from app.openclaw import gateway_admin, session_manager
    from app.channels.slack.config import get_binding_for_channel

    config = await resolve_goal_config(channel_id, team_id, pool)
    bot_token = config.get("bot_token") or "" if config else ""

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in to the control panel first, then try again."

    # Check platform_channel_bindings for this channel
    binding = await get_binding_for_channel(channel_id, team_id, pool)

    if binding and binding.get("actor_id"):
        # User SHCA channel
        goal_id = binding["goal_id"]
        thread_id = binding["thread_id"]
        agent_id = binding["agent_id"]
        if not thread_id or not agent_id:
            return "This assistant channel isn't fully wired up yet (no thread or agent linked)."

        from app.auth import get_goal_role
        if await get_goal_role(user_uid, goal_id) is None:
            return "You're not a member of this goal."

        label = f"your assistant *{agent_id[:8]}*"
        asyncio.create_task(run_followup(
            response_url, "Refresh",
            _reset_session(
                gateway_admin, session_manager,
                goal_id=goal_id, agent_id=agent_id, thread_id=thread_id,
                label=label, user_uid=user_uid, response_url=response_url,
            ),
        ))
        return f"🔄 Refreshing {label}…"

    # GO main channel or orchestrator channel
    if config is None:
        return (
            "I couldn't tell which agent this channel belongs to. "
            f"Run /{command} refresh from your assistant channel, the goal channel, "
            "or the orchestrator channel."
        )

    goal_id = config["goal_id"]
    from app.auth import get_goal_role
    if await get_goal_role(user_uid, goal_id) is None:
        return "You're not a member of this goal."

    goal = await pool.fetchrow(
        "SELECT go_agent_id, go_thread_id, name FROM goals WHERE id = $1", goal_id,
    )
    if not goal or not goal.get("go_agent_id") or not goal.get("go_thread_id"):
        return "This goal doesn't have a Goal Orchestrator wired up."

    label = f"Goal Orchestrator for *{goal['name']}*"
    asyncio.create_task(run_followup(
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
    *, goal_id: str, agent_id: str, thread_id: str, label: str,
    user_uid: str, response_url: str = "",
) -> None:
    pool = get_pool()
    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)

    session_key = make_goal_session_key(agent_id, goal_id, thread_id)
    await gateway_admin.reset_session(session_key)
    _log.info("Slack /refresh reset session: %s", session_key)

    try:
        from app.ledger.journal import append_event
        from app.ledger.schema import THREAD_REFRESHED
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    event_type=THREAD_REFRESHED,
                    payload={"thread_id": thread_id, "agent_id": agent_id, "reason": "slack_/refresh"},
                    actor_id=user_actor_id,
                    context_goal_id=goal_id,
                    context_conversation_id=thread_id,
                )
    except Exception as exc:
        _log.warning("Slack /refresh: ledger append failed: %s", exc)

    try:
        session_manager.evict_thread(goal_id, agent_id, thread_id)
    except Exception as exc:
        _log.warning(
            "Slack /refresh: evict_thread failed (goal=%s agent=%s thread=%s): %s",
            goal_id, agent_id, thread_id[:12] if thread_id else "-", exc,
        )

    await post_followup(
        response_url,
        f"🔄 Refreshed {label}. The next message will start a new session "
        f"with the latest RESOURCES.md / IDENTITY.md / MEMORY.md.",
    )