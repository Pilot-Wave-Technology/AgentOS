"""A finished story's channel goes away.

WHY A CHANNEL HAS TO BE RETIRED AT ALL. One channel per thread means a
goal accumulates one channel per story for ever. After twenty stories
the workspace sidebar is twenty dead conversations and the three live
ones are lost in them -- which is the same failure as having no
channels at all, arrived at from the other side.

ARCHIVED, NOT DELETED, AND THAT IS NOT A CHOICE WE MADE.

  SLACK HAS NO DELETE. Outside Enterprise Grid `conversations.delete`
  does not exist; `conversations.archive` is the whole of the API. An
  archived channel leaves the sidebar, stays searchable, and can be
  unarchived. Its NAME stays reserved for ever, which is why
  `naming.channel_name` keys on the thread id -- a second story with
  the same title would otherwise collide with the archived first one.

  AND THE PANEL THREAD IS EVIDENCE. `kernel/ledger` is hash-chained and
  `actors.py` derives `human_uid` from the thread's `created_by`;
  `DELETE FROM messages WHERE thread_id = $1` would destroy the record
  of who approved what, for every story ever completed, irreversibly.
  So the panel thread is closed, not dropped: it stops appearing as
  live work and every message in it survives.

TWO TRIGGERS, ON PURPOSE.

  IMMEDIATE, from `record_state`, so the channel goes as the story
  finishes rather than up to fifteen minutes later.

  AND A SWEEP, from the reconcile pass, because the immediate call runs
  on the edge of a transition that must not fail because Slack is
  unreachable -- so it is allowed to lose, and something has to catch
  what it loses.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# The states that mean "this story is over". `rejected` is here with
# the other three: a rejected story is as finished as a cancelled one.
TERMINAL = ("done", "reviewed", "cancelled", "rejected")


async def retire_thread(pool, thread_id: str) -> bool:
    """Archive this thread's Slack channel. True if one was archived.

    Never raises. Both callers are on paths where Slack being down must
    not break the thing that actually matters -- a state transition, or
    the rest of a reconcile pass.
    """
    try:
        from app.channels.slack.thread_channels import (
            archive_channel_for_thread)
        return bool(await archive_channel_for_thread(pool, thread_id))
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack: could not retire the channel for %s: %s",
                     (thread_id or "")[:12], exc)
        return False


async def retire_story(pool, story_id: str) -> int:
    """Retire every channel belonging to a finished story.

    EVERY thread, not just the active one. A story has at least two --
    the assistant's and the GO's narration thread -- and retiring only
    `active_thread_id` would leave the other one standing, which is
    exactly the sidebar clutter this exists to prevent.
    """
    if not story_id:
        return 0
    try:
        rows = await pool.fetch(
            """
            SELECT b.thread_id
              FROM platform_channel_bindings b
              JOIN threads t ON t.id = b.thread_id
             WHERE t.story_id = $1 AND b.archived_at IS NULL
            """, story_id)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack: could not list channels for story %s: %s",
                     story_id, exc)
        return 0

    done = 0
    for r in rows:
        if await retire_thread(pool, r["thread_id"]):
            done += 1
    if done:
        _log.info("Slack: retired %d channel(s) for finished story %s",
                  done, story_id)
    return done


async def sweep(pool, *, limit: int = 200) -> int:
    """The backstop: any live binding whose story is already over.

    Catches what the immediate call lost -- Slack unreachable at the
    moment of the transition, a state written before this code existed,
    or a story finished by a path that does not go through
    `record_state` at all.
    """
    try:
        rows = await pool.fetch(
            f"""
            SELECT b.thread_id
              FROM platform_channel_bindings b
              JOIN threads t ON t.id = b.thread_id
              JOIN stories s ON s.id = t.story_id
             WHERE b.archived_at IS NULL
               AND s.state = ANY($1::text[])
             ORDER BY b.updated_at ASC
             LIMIT {int(limit)}
            """, list(TERMINAL))
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack retire sweep: could not list: %s", exc)
        return 0

    done = 0
    for r in rows:
        if await retire_thread(pool, r["thread_id"]):
            done += 1
    if done:
        _log.info("Slack retire sweep: %d channel(s) retired", done)
    return done
