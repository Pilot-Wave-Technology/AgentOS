"""/story work — start working on a story from a Slack channel.

Only reachable as a /story subcommand (see story.py) — there is no standalone
/work slash command registered with the Slack App.
"""
import asyncio
import logging

import httpx

from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_config, resolve_user,
)

_log = logging.getLogger(__name__)


async def handle(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    response_url: str,
    command: str = "",
) -> str:
    if not command:
        from app.channels.slack.commands.registry import _cmd_prefix
        command = _cmd_prefix("story")
    if not args:
        return f"Usage: /{command} work <story title or short id>"

    pool = get_pool()
    config = await resolve_goal_config(channel_id, team_id, pool)
    if not config:
        return (
            "I couldn't figure out which goal this channel is linked to. "
            f"Please run /{command} work from the goal channel."
        )

    user_uid = await resolve_user(slack_user_id, config["bot_token"], pool)
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control panel "
            "with the same email first, then try again."
        )

    from app.auth import get_goal_role
    if await get_goal_role(user_uid, config["goal_id"]) is None:
        return "You're not a member of this goal."

    asyncio.create_task(_run_async(
        team_id=team_id, config=config,
        slack_user=slack_user_id, user_uid=user_uid,
        text=args, response_url=response_url,
    ))
    return f"Working on it — finding *{args[:60]}* and setting up your channel…"


async def _run_async(
    team_id: str, config: dict, slack_user: str, user_uid: str,
    text: str, response_url: str,
) -> None:
    try:
        from app.channels.slack.inbound import _handle_work_on_story
        pool = get_pool()
        await _handle_work_on_story(
            team_id=team_id,
            goal_id=config["goal_id"],
            binding={},
            slack_user=slack_user,
            user_uid=user_uid,
            text=text,
            pool=pool,
            response_url=response_url,
        )
    except Exception as exc:
        _log.error("Slash /work async flow failed: %s", exc, exc_info=True)
        if response_url:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(response_url, json={
                        "response_type": "ephemeral",
                        "replace_original": False,
                        "text": f"Something went wrong: {exc}",
                    })
            except Exception as _post_exc:
                _log.warning("Could not post error reply to response_url: %s", _post_exc)