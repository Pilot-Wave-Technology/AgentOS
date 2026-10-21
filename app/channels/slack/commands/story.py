"""/story — list, create, transition, complete, and review stories.

Subcommands:
  /story                                 list active stories
  /story <id>                            show detail for a story
  /story create <title>                  create a new story in 'proposed' state
  /story accept <id>                     claim + accept (proposed → accepted)
  /story start <id>                      start work (accepted → in_progress)
  /story block <id> <reason>             block a story with a reason
  /story unblock <id>                    unblock (blocked → in_progress)
  /story done <id> [feedback] [--force]  mark a story done
  /story open <title or id>              open this story's channel
  /story review <id> [approve|reject] [feedback]  clear a human-review checkpoint

All state transitions go through app.services.story_transitions — same guards as the web UI.
"""
import logging
import uuid

from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user, display_name,
)

_log = logging.getLogger(__name__)

# `start` IS NOT HERE ANY MORE. As a bare transition it set
# accepted -> in_progress and nothing else: no workspace, no assistant,
# no kickoff -- so somebody who typed `/story start` got a state change
# and no work, which is the most misleading thing a command can do.
# Starting a story means importing it, and that is what it does now.
_TRANSITION_ACTIONS = {"accept", "block", "unblock"}


async def handle(
    args: str, channel_id: str, team_id: str,
    slack_user_id: str = "", bot_token: str = "",
    response_url: str = "",
    command: str = "",
) -> str:
    cmd = command or "story"
    pool = get_pool()
    goal_id, resolved_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return (
            f"Run /{cmd} from a goal channel, an orchestrator channel, "
            "a story channel, or your assistant channel."
        )
    effective_token = bot_token or resolved_token

    first_word = (args or "").split()[0].lower() if args else ""

    if first_word in ("help", "--help", "-h"):
        from app.channels.slack.commands.registry import render_command_help
        return render_command_help("story")

    # Subcommand: open — the channel FOR THIS STORY.
    #
    # RENAMED FROM `work`. Its help said "open your personal assistant
    # channel for a specific story" -- singular, one channel per member,
    # the story rotating through it. That is the model one-channel-per-
    # thread replaces, so the operation is now "create or open the
    # channel for this story": the same words would have meant something
    # different, which is the kind of change that generates support
    # questions for a year. `work` still answers, with a pointer.
    # ONE ROUTE. `/story start` accepts the story if it is still
    # proposed, assigns it to the caller, provisions their assistant's
    # workspace and opens the story's channel -- everything that
    # "accept it and import it to my assistant" means, in one command.
    #
    # It was briefly four words for the same thing (start / open /
    # import / work). Four spellings of one route is three more ways to
    # document it and three more to get wrong; the old names answer with
    # a pointer so nobody mid-flight is left guessing, and that is all.
    if first_word in ("open", "import", "work"):
        return f"That's `/{cmd} start <id>` now — it accepts the story and "                f"sets up your assistant in one step."

    # CANCEL IS ONE ROUTE TOO, and the same shape as start: the whole
    # thing in one command, through the one implementation.
    #
    # `_cancel_story` is what the board calls. It carries the invariant
    # that a STARTED code story is never cancelled (its branch is cut and
    # work may be committed; cancelling would strand it), bridges the
    # dependents so nothing is left waiting on a story that will never
    # arrive, re-derives the topology, writes the ledger event and
    # notifies the assignee. A Slack-side reimplementation would carry
    # none of that -- and the easy version, flipping state to
    # 'cancelled', would silently strand every story that depended on it.
    if first_word == "cancel":
        rest = args[len("cancel"):].strip()
        if not rest:
            return f"Usage: /{cmd} cancel <id> [reason]"
        bits = rest.split(None, 1)
        id_prefix, reason = bits[0], (bits[1].strip() if len(bits) > 1 else "")
        return await _cancel_from_slack(goal_id, id_prefix, reason,
                                        slack_user_id, effective_token, pool,
                                        cmd=cmd)

    if first_word == "start":
        rest = args[len("start"):].strip()
        if not rest:
            return f"Usage: /{cmd} start <story title or short id>"
        from app.channels.slack.commands.work import handle as _h
        return await _h(rest, slack_user_id, channel_id, team_id, response_url,
                        command=cmd)

    # Subcommand: done — mark a story done (with optional --force and feedback)
    if first_word == "done":
        rest = args[len("done"):].strip()
        from app.channels.slack.commands.done import handle as _h
        return await _h(rest, slack_user_id, channel_id, team_id, response_url, command=cmd)

    # Subcommand: review — clear a human-review checkpoint
    if first_word == "review":
        rest = args[len("review"):].strip()
        from app.channels.slack.commands.human_review import handle as _h
        return await _h(rest, slack_user_id, channel_id, team_id, response_url, command=cmd)

    # Membership check for everything below (create/transition/list/detail) —
    # `work`/`done`/`review` were already dispatched above and gate themselves.
    from app.auth import get_goal_role
    requester_uid = await resolve_user(slack_user_id, effective_token, pool)
    if not requester_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."
    if await get_goal_role(requester_uid, goal_id) is None:
        return "You're not a member of this goal."

    # Subcommand: create
    if first_word == "create":
        title = args[len("create"):].strip()
        if not title:
            return f"Usage: /{cmd} create <title>"
        return await _create_story(goal_id, title, slack_user_id, effective_token, pool)

    # Subcommand: state transition (accept/start/block/unblock)
    if first_word in _TRANSITION_ACTIONS:
        rest = args[len(first_word):].strip()
        return await _transition_story(
            goal_id, first_word, rest, slack_user_id, effective_token, pool, cmd=cmd,
        )

    if args:
        row = await pool.fetchrow("""
            SELECT id, title, description, state, assigned_to, priority,
                   blocked_reason, progress_summary, created_at
              FROM stories
             WHERE goal_id = $1 AND (id = $2 OR id LIKE $3)
             ORDER BY (id = $2) DESC, created_at DESC
             LIMIT 1
        """, goal_id, args.strip(), f"{args.strip()[:8]}%")
        if not row:
            return f"No story found for {args[:30]}."
        who = await display_name(pool, row.get("assigned_to", "")) or "unassigned"
        lines = [
            f"*{row['title']}* ({row['id'][:8]})",
            f"state: *{row['state']}* · priority: *{row['priority']}* · assigned to: *{who}*",
        ]
        if row.get("description"):
            lines.append(f"_{row['description'][:500]}_")
        if row.get("blocked_reason"):
            lines.append(f"⚠️ _Blocked:_ {row['blocked_reason'][:300]}")
        if row.get("progress_summary"):
            lines.append(f"📌 _Progress:_ {row['progress_summary'][:500]}")
        return "\n".join(lines)

    rows = await pool.fetch("""
        SELECT id, title, state, assigned_to FROM stories
         WHERE goal_id = $1 AND state NOT IN ('cancelled', 'rejected')
         ORDER BY CASE state
                    WHEN 'in_progress' THEN 0
                    WHEN 'blocked' THEN 1
                    WHEN 'accepted' THEN 2
                    WHEN 'proposed' THEN 3
                    WHEN 'done' THEN 4
                    ELSE 5 END, created_at DESC
         LIMIT 20
    """, goal_id)
    if not rows:
        return "No stories in this goal yet."
    lines = ["*Stories:*"]
    for r in rows:
        who = await display_name(pool, r.get("assigned_to", "")) or "unassigned"
        lines.append(f"• {r['id'][:8]} *{r['state']}* · {r['title']} — {who}")
    return "\n".join(lines)


async def _transition_story(
    goal_id: str, action: str, rest: str, slack_user_id: str, bot_token: str, pool,
    cmd: str = "story",
) -> str:
    """Resolve the story by id-prefix and apply a state transition via the service.

    For `block`, the first token of `rest` is the story id and the remainder is
    the blocked reason; for every other action `rest` is just the id.
    """
    if not rest:
        usage = (
            f"Usage: /{cmd} block <id> <reason>" if action == "block"
            else f"Usage: /{cmd} {action} <id>"
        )
        return usage

    if action == "block":
        parts = rest.split(None, 1)
        id_prefix = parts[0]
        blocked_reason = parts[1].strip() if len(parts) > 1 else ""
        if not blocked_reason:
            return f"Usage: /{cmd} block <id> <reason> — a reason is required to block."
    else:
        id_prefix = rest.split()[0]
        blocked_reason = ""

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."

    story = await pool.fetchrow(
        """SELECT id, title, state FROM stories
            WHERE goal_id = $1 AND (id = $2 OR id LIKE $3)
            ORDER BY (id = $2) DESC, created_at DESC LIMIT 1""",
        goal_id, id_prefix, f"{id_prefix[:8]}%",
    )
    if not story:
        return f"No story in this goal matched `{id_prefix[:30]}`."

    from app.services.story_transitions import apply_story_transition
    ok, reason = await apply_story_transition(
        goal_id, story["id"], action, user_uid, pool, blocked_reason=blocked_reason,
    )
    if not ok:
        _log.info(
            "Slack /story %s rejected: goal=%s story=%s uid=%s reason=%s",
            action, goal_id, story["id"], user_uid, reason,
        )
        current = story["state"]
        _VALID_FROM = {
            "proposed":    ["accept"],
            "accepted":    ["start"],
            "in_progress": ["block", "done"],
            "blocked":     ["unblock", "done"],
        }
        hint = ""
        valid = _VALID_FROM.get(current)
        if valid:
            hint = f" Story is *{current}* — valid actions: {', '.join(f'`{a}`' for a in valid)}."
        return f"Could not {action} story `{story['id'][:8]}`: {reason}.{hint}"

    _log.info(
        "Slack /story %s: goal=%s story=%s by=%s",
        action, goal_id, story["id"], user_uid,
    )
    verb = {
        "accept": "accepted", "start": "started", "block": "blocked",
        "unblock": "unblocked",
    }[action]
    suffix = f"\n> {blocked_reason}" if blocked_reason else ""
    return f"✅ Story `{story['id'][:8]}` *{story['title']}* {verb}.{suffix}"


async def _create_story(
    goal_id: str, title: str, slack_user_id: str, bot_token: str, pool,
) -> str:
    """Insert a new story in 'proposed' state, emit ledger event, relay lifecycle."""
    from app.channels.slack.commands._helpers import resolve_user

    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."

    story_id = str(uuid.uuid4())[:8]
    thread_id = str(uuid.uuid4())

    from app.ledger.journal import append_event
    from app.ledger.actors import human_actor
    # The Slack user who ran /story create is the actor (system if unresolved).
    _sa_actor, _sa_type = await human_actor(user_uid)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("""
            INSERT INTO stories (id, goal_id, title, description, acceptance_criteria,
                                 assigned_to, priority, estimated_effort, state,
                                 thread_id, created_by)
            VALUES ($1, $2, $3, '', '', '', 'medium', '', 'proposed', $4, $5)
        """, story_id, goal_id, title, thread_id, user_uid)

        await conn.execute("""
            INSERT INTO threads (id, goal_id, created_by, title)
            VALUES ($1, $2, $3, $4)
        """, thread_id, goal_id, user_uid, f"Story: {title[:80]}")

        await append_event(
            conn,
            actor_id=_sa_actor, actor_type=_sa_type,
            event_type="story.created",
            payload={
                "story_id": story_id,
                "title": title,
                "assigned_user_id": "",
                "priority": "medium",
                "thread_id": thread_id,
                "actor_uid": user_uid,
            },
            context_goal_id=goal_id,
        )

    try:
        from app.channels.base import relay_lifecycle_event
        await relay_lifecycle_event(goal_id, "story.created", {
            "story_id": story_id,
            "story_title": title,
            "assigned_to": "",
            "priority": "medium",
        })
    except Exception as exc:
        _log.warning(
            "story.created lifecycle relay failed for %s: %s", story_id, exc,
        )

    _log.info(
        "Slack /story create: goal=%s story=%s title=%r by=%s",
        goal_id, story_id, title, user_uid,
    )
    return f"✅ Story `{story_id}` *{title}* created (proposed)."

async def _cancel_from_slack(goal_id: str, id_prefix: str, reason: str,
                             slack_user_id: str, bot_token: str, pool,
                             cmd: str = "story") -> str:
    """Cancel through `_cancel_story`, the same path the board uses."""
    user_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not user_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."

    from app.auth import get_goal_role
    if await get_goal_role(user_uid, goal_id) is None:
        return "You're not a member of this goal."

    story = await pool.fetchrow(
        """SELECT id, title, state, assigned_to FROM stories
            WHERE goal_id = $1 AND (id = $2 OR id LIKE $3)
            ORDER BY (id = $2) DESC, created_at DESC LIMIT 1""",
        goal_id, id_prefix, f"{id_prefix[:8]}%")
    if not story:
        return f"No story in this goal matched `{id_prefix[:30]}`."

    # BEING IN THE GOAL IS NOT THE SAME AS THE STORY BEING YOURS.
    # `_cancel_story` checks membership and nothing narrower, so without
    # this a colleague could throw away somebody else's work.
    from app.channels.slack.commands._entitled import may_cancel
    ok, why = await may_cancel(pool, user_uid, goal_id, dict(story))
    if not ok:
        return why

    from app.routers.stories import _cancel_story
    result = await _cancel_story(
        goal_id, story["id"], reason or "Cancelled from Slack", user_uid)

    # It answers with a JSONResponse, because its other callers are
    # routes. Read the body rather than guessing from the status.
    import json as _json
    body = {}
    try:
        body = _json.loads(bytes(getattr(result, "body", b"") or b"{}"))
    except Exception:                                          # noqa: BLE001
        pass
    if body.get("ok") is False:
        # The commonest refusal is the started-code-story invariant, and
        # its message says what to do instead. Pass it through rather
        # than flattening it to "couldn't".
        return body.get("error") or "That story could not be cancelled."
    return (f"Cancelled *{story['title']}* (`{story['id'][:8]}`)."
            + (f" Reason: {reason}" if reason else ""))
