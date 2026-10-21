"""A Slack channel per internal thread, found-or-created, idempotently.

THE BUG THIS EXISTS TO FIX IS LIVE AND SILENT. The internal model moved
to one thread per story (`app/services/story_threads.py`), but
`platform_channel_bindings` held one row per (goal, actor) with
`thread_id` rotating underneath -- and only the Slack `/work` path ever
repointed it. The web import path and the git-engine path never did. So
from a member's SECOND story onward:

  * outbound: `get_binding_by_thread()` returns None, `outbound.py`
    logs at DEBUG and drops the assistant's reply
  * inbound: a human's message routes to `binding.thread_id`, which is
    still the FIRST story's thread -- so it lands in the wrong
    conversation

Neither produces an error. The fix is structural: a thread that should
be visible in Slack gets its OWN binding, and the binding is never
repointed at a different thread.

Private from the outset. Phase 6 brings the existing goal channels over
and adds membership sync; there is no reason for anything created from
here to start public and need converting, especially since Slack cannot
convert a public channel to private at all.
"""

from __future__ import annotations

import logging

from app.channels.slack.naming import channel_name

_log = logging.getLogger(__name__)


async def _slack_for_goal(pool, goal_id: str) -> tuple[str, str] | None:
    """`(integration_id, bot_token)` if Slack is live for this goal.

    Honours the per-goal switch: a goal with no `goal_platform_integrations`
    row, or one with `enabled = false`, gets no channels. That is the
    opt-in, and it has to be checked here or enabling Slack would be
    retroactive the moment anything called this.
    """
    row = await pool.fetchrow(
        """
        SELECT pi.id, pi.credentials_json
          FROM goal_platform_integrations gpi
          JOIN platform_integrations pi ON pi.id = gpi.integration_id
         WHERE gpi.goal_id = $1 AND gpi.enabled = true
           AND pi.platform = 'slack' AND pi.status = 'active'
         LIMIT 1
        """, goal_id)
    if not row:
        return None
    creds = row["credentials_json"]
    if isinstance(creds, str):
        import json
        creds = json.loads(creds) if creds else {}
    token = (creds or {}).get("bot_token", "")
    if not token:
        _log.warning("Slack: goal %s is enabled but has no bot token", goal_id)
        return None
    return str(row["id"]), token


async def is_slack_excluded(pool, goal_id: str, thread_id: str) -> bool:
    """Threads that stay in the panel and never reach Slack.

    THE GOAL REVIEW COUNCIL, THE SECURITY COUNCIL AND THE AGENT MESH
    (the orchestrator channel) are read-only machine logs: councils
    reporting their findings, agents coordinating with each other.
    Nobody answers them in Slack, so a channel each is three more
    channels per goal that exist to be muted -- and at one channel per
    thread the whole point is that a channel is somewhere a person
    might actually say something.

    The GO's own thread is NOT excluded: that is the conversation with
    the Goal Orchestrator, and it is one of the main reasons for having
    Slack at all.

    Enforced here rather than at each caller because this is the single
    chokepoint every channel passes through -- `ensure_story_thread`,
    `ensure_go_story_thread`, the errand path and the back-fill all come
    here, and a rule applied at four call sites is a rule with four
    chances to be forgotten.
    """
    row = await pool.fetchrow(
        """SELECT orchestrator_thread_id, council_thread_id,
                  security_thread_id
             FROM goals WHERE id = $1""", goal_id)
    if row is None:
        return False
    if thread_id in {v for v in dict(row).values() if v}:
        _log.info("Slack: thread %s is a council/mesh channel — no Slack "
                  "channel by design", thread_id[:12])
        return True
    return False


async def existing_binding(pool, integration_id: str, thread_id: str):
    return await pool.fetchrow(
        """
        SELECT id, external_channel_id, channel_name
          FROM platform_channel_bindings
         WHERE integration_id = $1::uuid AND thread_id = $2
           AND archived_at IS NULL
        """, integration_id, thread_id)


async def ensure_channel_for_thread(pool, *, goal_id: str, thread_id: str,
                                    title: str = "", actor_id: str | None = None,
                                    agent_id: str = "",
                                    story_id: str | None = None) -> str | None:
    """The channel id for this thread, creating it if there is not one.

    Returns None when Slack is off for the goal, which is the common
    case and not an error.

    Idempotent in the way that matters: the unique index on
    (integration_id, thread_id) means two concurrent callers cannot both
    end up with a binding, and the loser adopts the winner's channel
    rather than creating a second one.
    """
    if not thread_id:
        return None

    if await is_slack_excluded(pool, goal_id, thread_id):
        return None

    live = await _slack_for_goal(pool, goal_id)
    if live is None:
        return None
    integration_id, bot_token = live

    found = await existing_binding(pool, integration_id, thread_id)
    if found:
        return found["external_channel_id"]

    from app.channels.slack.client import make_async_client
    from app.channels.slack.lifecycle import (_channel_env_suffix,
                                              require_env_separation)

    require_env_separation()
    name = channel_name(goal_id=goal_id, thread_id=thread_id, title=title,
                        env_suffix=_channel_env_suffix().lstrip("-"))

    client = make_async_client(bot_token)
    try:
        created = await client.conversations_create(name=name, is_private=True)
        channel_id = created["channel"]["id"]
        realised = created["channel"].get("name", name)
    except Exception as exc:                                   # noqa: BLE001
        # No -2/-3 retry. The name carries the thread id, which is unique
        # by construction, so a collision means something we do not
        # understand -- and guessing a second name would hide it.
        from app.channels.slack.client import on_failure
        await on_failure(exc, pool=pool, integration_id=integration_id,
                         where="create channel for thread")
        _log.error("Slack: could not create channel %s for thread %s: %s",
                   name, thread_id[:12], exc)
        return None

    row = await pool.fetchrow(
        """
        INSERT INTO platform_channel_bindings
            (goal_id, integration_id, actor_id, external_channel_id,
             channel_name, resource_kind, thread_id, agent_id, current_story_id)
        VALUES ($1, $2::uuid, $3::uuid, $4, $5, 'channel', $6, $7, $8)
        ON CONFLICT (integration_id, thread_id)
            WHERE archived_at IS NULL AND thread_id IS NOT NULL
        DO NOTHING
        RETURNING id, external_channel_id
        """,
        goal_id, integration_id, actor_id, channel_id, realised,
        thread_id, agent_id or None, story_id)

    if row is None:
        # Somebody else bound this thread between our lookup and our
        # insert. Theirs is the real channel; ours is an orphan, so say so
        # rather than leaving a channel nobody can find.
        other = await existing_binding(pool, integration_id, thread_id)
        _log.warning(
            "Slack: thread %s was bound concurrently; channel %s is orphaned",
            thread_id[:12], channel_id)
        return other["external_channel_id"] if other else None

    _log.info("Slack: created #%s (%s) for thread %s", realised, channel_id,
              thread_id[:12])

    # AND PUT THE PEOPLE IN IT. Creating the channel and leaving it empty
    # was the whole of this function until now: the bot sat alone in a
    # private channel, and the only thing that would ever add anybody was
    # the reconcile pass, up to fifteen minutes later. So a story
    # imported to its assignee produced a channel that assignee could not
    # even see -- Slack does not show a non-member a private channel --
    # and the feature looked broken at exactly the moment somebody first
    # tried it.
    #
    # `sync_channel_members` reads `threads.participants`: named
    # participants for a story thread, and for a GO or errand thread --
    # where the list is empty -- every member of the goal, which is what
    # makes an errand visible to the people who might answer it.
    #
    # kick=False: nobody can be in a channel created a moment ago, so
    # there is nothing to remove, and asking Slack to check costs a call.
    try:
        from app.channels.slack.membership import sync_channel_members
        out = await sync_channel_members(pool, thread_id, kick=False)
        if out.get("skipped"):
            _log.info(
                "Slack: %d person(s) in thread %s have no confirmed Slack "
                "mapping and were not invited", out["skipped"], thread_id[:12])
    except Exception as exc:                                   # noqa: BLE001
        # The channel exists and is bound; an invite that failed is
        # recoverable by the reconcile pass, so this must not undo it.
        _log.warning("Slack: could not populate #%s: %s", realised, exc)

    return channel_id


async def archive_channel_for_thread(pool, thread_id: str) -> bool:
    """Thread is finished: archive its channel and its binding.

    Slack has no `conversations.delete` outside Enterprise Grid, so
    "deleted" means archived -- and an archived channel KEEPS ITS NAME
    reserved, which is why the name carries the thread id rather than
    the story id.

    The binding is stamped whether or not Slack cooperates. A channel we
    cannot archive is untidy; a binding that stays live points the relay
    at a channel nobody is reading.
    """
    if not thread_id:
        return False
    row = await pool.fetchrow(
        """
        SELECT b.id, b.external_channel_id, b.integration_id, pi.credentials_json
          FROM platform_channel_bindings b
          JOIN platform_integrations pi ON pi.id = b.integration_id
         WHERE b.thread_id = $1 AND b.archived_at IS NULL
        """, thread_id)
    if not row:
        return False

    creds = row["credentials_json"]
    if isinstance(creds, str):
        import json
        creds = json.loads(creds) if creds else {}
    token = (creds or {}).get("bot_token", "")
    if token and row["external_channel_id"]:
        from app.channels.slack.client import make_async_client
        try:
            await make_async_client(token).conversations_archive(
                channel=row["external_channel_id"])
        except Exception as exc:                               # noqa: BLE001
            # already_archived is the common one and is a success.
            from app.channels.slack.client import error_code
            if error_code(exc) != "already_archived":
                _log.warning("Slack: could not archive channel %s: %s",
                             row["external_channel_id"], exc)

    await pool.execute(
        "UPDATE platform_channel_bindings SET archived_at = now(), "
        "updated_at = now() WHERE id = $1", row["id"])
    _log.info("Slack: archived channel for thread %s", thread_id[:12])
    return True
