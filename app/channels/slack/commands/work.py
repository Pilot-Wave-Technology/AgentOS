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
    # RESOLVED FROM THE BINDING, like /run, /park and /errand.
    # `resolve_goal_config` requires `pcb.actor_id IS NULL`, which is true
    # of a goal channel and of a GO story channel and NOT of an
    # assistant's own story channel -- so starting a story from the place
    # you are most likely to be standing answered "I couldn't figure out
    # which goal this channel is linked to", and told you to go somewhere
    # else. The binding knows, in every one of those channels.
    from app.channels.slack.commands._context import resolve as _resolve_ctx
    ctx = await _resolve_ctx(channel_id, team_id, pool)
    if not ctx.ok:
        return (
            "I couldn't work out which goal this channel belongs to. "
            f"Try /{command} start from the goal's channel."
        )
    config = await resolve_goal_config(channel_id, team_id, pool) or {}
    config = {
        "goal_id": ctx.goal_id,
        "bot_token": ctx.bot_token or config.get("bot_token", ""),
        "bot_user_id": config.get("bot_user_id", ""),
        "workspace_id": config.get("workspace_id", team_id),
        # The goal channel is where the work-on-story flow posts its
        # progress; a story channel does not have one of its own.
        "goal_channel_id": config.get("goal_channel_id", ""),
    }

    user_uid = await resolve_user(slack_user_id, config["bot_token"], pool)
    if not user_uid:
        return (
            "I don't recognise your Slack account. Sign in to the control panel "
            "with the same email first, then try again."
        )

    from app.auth import get_goal_role
    if await get_goal_role(user_uid, config["goal_id"]) is None:
        return "You're not a member of this goal."

    # CHECK ENTITLEMENT HERE, synchronously, so the refusal is the slash
    # command's own answer.
    #
    # The flow already refuses a story held by somebody else -- but it
    # does it deep inside the backgrounded part, as an ephemeral posted
    # into the GOAL channel. Run from a story channel, the person saw
    # "Working on it…" here and the refusal somewhere they were not
    # looking, so the story simply never started and nothing said why.
    story = await pool.fetchrow(
        """SELECT id, title, assigned_to FROM stories
            WHERE goal_id = $1
              AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
              AND (id = $2 OR id LIKE $3 OR lower(title) LIKE $4)
            ORDER BY (id = $2) DESC, created_at DESC LIMIT 1""",
        config["goal_id"], args.strip(), f"{args.strip()[:8]}%",
        f"%{args.strip().lower()}%")
    if story:
        from app.channels.slack.commands._entitled import may_start
        ok, why = await may_start(pool, user_uid, dict(story))
        if not ok:
            return why

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