"""What a channel already knows, so a command need not be told it.

THE DIVIDEND OF ONE CHANNEL PER THREAD. A command typed in a story's
channel is unambiguous about which story it means, and asking the
person to type an id they can see in the channel name is the kind of
small friction that stops people using a thing at all. `/park answer
yes` beats `/park answer 4182 yes` every time.

So ids are OPTIONAL everywhere and REQUIRED nowhere the channel can
answer for itself. In a goal channel -- which is about no single story
-- an id is the only way to say which one, and the commands ask for it
there.

Designing this in from the start rather than retrofitting it matters:
a command written to take an id and later taught to infer one ends up
supporting both forms, documenting neither, and behaving differently
depending on where you typed it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelContext:
    """Everything a command can learn from where it was typed."""

    goal_id: str = ""
    thread_id: str = ""
    story_id: str = ""
    bot_token: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.goal_id)

    @property
    def is_story_channel(self) -> bool:
        return bool(self.story_id)


async def resolve(channel_id: str, team_id: str, pool) -> ChannelContext:
    """The goal, thread and story this channel is about.

    Reads the BINDING, which is the identity -- never the channel name,
    which is decoration and may be stale or renamed by hand.
    """
    if not channel_id:
        return ChannelContext()

    row = await pool.fetchrow(
        """
        SELECT b.goal_id, b.thread_id, b.current_story_id, t.story_id,
               pi.credentials_json
          FROM platform_channel_bindings b
          JOIN platform_integrations pi ON pi.id = b.integration_id
          LEFT JOIN threads t ON t.id = b.thread_id
         WHERE b.external_channel_id = $1
           AND b.archived_at IS NULL
           AND pi.platform = 'slack'
           AND pi.status = 'active'
         LIMIT 1
        """, channel_id)

    if row is None:
        # Not a bound channel. The legacy per-goal resolver still knows
        # about goal and orchestrator channels, which have no binding of
        # their own on older goals.
        from app.channels.slack.commands._helpers import (
            resolve_goal_from_any_channel)
        goal_id, token = await resolve_goal_from_any_channel(
            channel_id, team_id, pool)
        return ChannelContext(goal_id=goal_id or "", bot_token=token or "")

    creds = row["credentials_json"]
    if isinstance(creds, str):
        import json
        creds = json.loads(creds) if creds else {}

    # `threads.story_id` is the thread's own story and is authoritative.
    # `current_story_id` is the binding's rotating pointer, kept for the
    # legacy per-member channels where one channel really did serve many
    # stories in turn.
    story = row["story_id"] or row["current_story_id"] or ""
    return ChannelContext(
        goal_id=row["goal_id"] or "",
        thread_id=row["thread_id"] or "",
        story_id=story,
        bot_token=(creds or {}).get("bot_token", ""),
    )


async def story_for(ctx: ChannelContext, args: str, pool) -> tuple[str, str]:
    """`(story_id, error)` — the story a command should act on.

    An explicit id in the arguments always wins, so somebody in a story
    channel can still refer to another story without moving. Otherwise
    the channel answers. A short id is resolved by prefix, because an
    8-character id is what the product shows people and what they will
    type.
    """
    token = (args or "").strip().split(" ", 1)[0].strip()
    if token and not token.startswith("<"):
        row = await pool.fetchrow(
            "SELECT id FROM stories WHERE goal_id = $1 AND id LIKE $2 "
            " ORDER BY (id = $3) DESC LIMIT 2",
            ctx.goal_id, f"{token}%", token)
        if row:
            return row["id"], ""

    if ctx.story_id:
        return ctx.story_id, ""

    return "", ("Which story? This channel isn't about one, so name it: "
                "the 8-character id from `/story`.")
