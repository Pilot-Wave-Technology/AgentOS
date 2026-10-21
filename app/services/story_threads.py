"""One thread per story, resolved the same way whoever asks.

WHY THIS IS A SERVICE AND NOT A ROUTER HELPER. It was
`app/routers/assistant.py::_ensure_story_thread`, and the local lane could
not import it -- "a service never imports a router" -- so
`local_assistant.bind_local_story_thread` grew its own resolver instead. The
two then disagreed about the most important thing: the gateway keyed threads
on the STORY, and the local lane kept one thread per (goal, user) and
DETACHED every other story bound to it.

    UPDATE stories SET is_active_focus = false, active_thread_id = NULL
     WHERE active_thread_id = $1 AND id <> $2

Importing a third story silently unbound the first two. Their turns then
failed `no_active_thread`, their recent-messages came back empty, and their
diffs submitted against a NULL thread. The router's own docstring named the
bug the local path still had -- "the one-story-per-thread leak that had to be
fixed once already".

So the resolver moves here and both callers use it. `threads.story_id` has
been in the schema all along.
"""

import logging
import uuid

logger = logging.getLogger(__name__)


async def ensure_story_thread(
    pool, user_uid: str, goal_id: str, agent_id: str, story_id: str,
) -> str:
    """This story's own thread. Find-or-create, keyed on the story.

    Re-importing returns the SAME thread and its history rather than starting
    a second empty one, which is what makes a story's conversation readable
    after it has been put down and picked up again.
    """
    existing = await pool.fetchval(
        """
        SELECT id FROM threads
         WHERE goal_id = $1 AND agent_id = $2 AND story_id = $3
           AND created_by = $4 AND left_goal_at IS NULL
         ORDER BY created_at DESC LIMIT 1
        """,
        goal_id, agent_id, story_id, user_uid)
    if existing:
        return existing

    # THE LABEL, NOT THE ID. Eight hex characters at the front push the
    # readable part of a title past the truncation on every sidebar row.
    # `label_for` prefers the six-word short title, falls back to the first
    # words of the real one, and reaches the id only when a story has
    # neither. The id is still on the tooltip and still addresses the story
    # everywhere else.
    from kernel.go.short_title import label_for  # noqa: PLC0415

    row = await pool.fetchrow(
        "SELECT title, short_title FROM stories WHERE id = $1", story_id)
    title = label_for(row["short_title"] if row else "",
                      row["title"] if row else "", story_id)
    thread_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO threads (id, goal_id, agent_id, story_id, created_by,
                             title, participants, status)
        VALUES ($1, $2, $3, $4, $5, $6, ARRAY[$5]::text[], 'active')
        """,
        thread_id, goal_id, agent_id, story_id, user_uid, title[:120])
    logger.info("opened story thread %s for %s/%s (%s)",
                thread_id, goal_id, story_id, agent_id)

    # Give it a Slack channel if the goal has Slack on. Every path that
    # opens a story thread comes through here, which is the point: the
    # live bug was that only ONE of the three import paths bound a
    # channel, so from the second story onward the relay had nothing to
    # post into and dropped the reply at DEBUG.
    try:
        from app.channels.slack.thread_channels import ensure_channel_for_thread
        actor_id = await pool.fetchval(
            "SELECT actor_id FROM users WHERE uid = $1", user_uid)
        await ensure_channel_for_thread(
            pool, goal_id=goal_id, thread_id=thread_id, title=title,
            actor_id=str(actor_id) if actor_id else None,
            agent_id=agent_id, story_id=story_id)
    except Exception as exc:
        # Slack is a mirror. Failing to mirror must not fail the thread.
        logger.warning("Slack channel for thread %s not created: %s",
                       thread_id, exc)

    # NO GO THREAD FOR AN ORDINARY STORY. There was one here, opened
    # beside the assistant's, so the GO had a per-story place to narrate
    # instead of interleaving twenty stories in one goal thread. It is
    # gone, by decision: a second thread per story meant a second Slack
    # channel per story, and the GO's goal thread is where its
    # commentary belongs.
    #
    # THE GO STILL GETS ITS OWN THREAD FOR AN AUTONOMOUS ERRAND, which
    # is a different case and lives in `errand_grants._open_story_thread`
    # -- nobody has an assistant conversation about an errand they did
    # not ask for, so without a thread of its own it has nowhere to go.

    return thread_id
