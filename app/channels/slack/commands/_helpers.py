"""Shared helpers for Slack slash command handlers.

No imports from app.slack.* — fully self-contained in the new channel system.
"""
import logging
from typing import Optional

import httpx

from app.database import get_pool

_log = logging.getLogger(__name__)


async def post_followup(response_url: str, text: str) -> None:
    """Post an ephemeral follow-up to a slash command's response_url."""
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


async def run_followup(response_url: str, label: str, coro) -> None:
    """Await coro and surface any exception via response_url."""
    try:
        await coro
    except Exception as exc:
        _log.error("Slack followup '%s' failed: %s", label, exc, exc_info=True)
        await post_followup(response_url, f"⚠️ {label} hit an error: {exc}")


async def resolve_goal_config(channel_id: str, team_id: str, pool) -> Optional[dict]:
    """Find the goal config for a main goal channel or orch channel."""
    from app.channels.slack.config import get_goal_by_channel, find_goal_by_orch_channel
    cfg = await get_goal_by_channel(team_id, channel_id, pool)
    if cfg:
        return cfg
    return await find_goal_by_orch_channel(team_id, channel_id, pool)


async def resolve_goal_from_any_channel(
    channel_id: str, team_id: str, pool,
) -> tuple[str, str]:
    """Resolve (goal_id, bot_token) from any Slack channel surface.

    Checks main goal channel, orch channel, then any platform_channel_bindings
    row (SHCA user channels). Returns ("", "") if nothing matches.
    """
    cfg = await resolve_goal_config(channel_id, team_id, pool)
    if cfg:
        return cfg["goal_id"], cfg.get("bot_token") or ""

    from app.channels.slack.config import get_binding_for_channel
    binding = await get_binding_for_channel(channel_id, team_id, pool)
    if binding:
        return binding["goal_id"], binding.get("bot_token") or ""

    return "", ""


async def resolve_user(
    slack_user_id: str, bot_token: str, pool,
) -> str:
    """Resolve Slack user ID → control-panel UID, auto-mapping by email if needed."""
    if not slack_user_id:
        return ""
    from app.channels.slack.thread_map import get_user_uid, auto_map_by_email
    uid = await get_user_uid(slack_user_id, pool)
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
            return await auto_map_by_email(slack_user_id, email, display_name, pool=pool) or ""
    except Exception as exc:
        _log.warning("Failed to resolve Slack user %s: %s", slack_user_id, exc)
    return ""


async def display_name(pool, uid: str) -> str:
    if not uid:
        return ""
    try:
        return await pool.fetchval("SELECT display_name FROM users WHERE uid = $1", uid) or ""
    except Exception:
        return ""