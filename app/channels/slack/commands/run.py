"""`/run` — see and steer an execution from Slack.

THE EXECUTION LAYER HAD NO COMMAND AT ALL. The assistant runs on
executions, legs, steps and parks -- a live story was observed at
"execution 16, step 222, leg 13/14", parked on an irreversible-call
approval -- and none of it was reachable from Slack. Nine capabilities
the panel exposes had no slash command, and the three that existed as
buttons were inert in production because the app still pointed at the
old endpoint. So the layer where work actually happens was visible
from a browser and nowhere else.

Everything here calls the SAME services the panel calls. A second
implementation of pause, cancel or more-work would be a second place
for the kill switch to be forgotten.
"""

from __future__ import annotations

import logging

from app.channels.slack.commands._context import resolve, story_for

_log = logging.getLogger(__name__)

USAGE = (
    "/run — what this story's execution is doing\n"
    "\n"
    "  /run [<story-id>]      status: state, leg, step, and anything parked\n"
    "  /run pause [why]       stop at the next step boundary; Resume carries on\n"
    "  /run resume [what]     carry on; anything you write is a STEER\n"
    "  /run retry             retry a failed execution\n"
    "  /run cancel [why]      stop it for good\n"
    "  /run more <what>       queue another cycle of work\n"
    "\n"
    "In a story's own channel the id is optional — the channel knows.\n"
    "pause, resume and cancel open a box for the text if you leave it off."
)


async def handle(args: str, channel_id: str, team_id: str, *,
                 slack_user_id: str = "", response_url: str = "",
                 command: str = "run", trigger_id: str = "") -> str:
    from app.channels.slack.thread_map import get_user_uid
    from app.database import get_pool

    pool = get_pool()
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if sub in ("help", "-h", "--help"):
        return USAGE

    ctx = await resolve(channel_id, team_id, pool)
    if not ctx.ok:
        return ("This channel isn't connected to a goal. Run it from a goal "
                "or story channel.")

    user_uid = await get_user_uid(slack_user_id, pool=pool)
    if not user_uid:
        return ("I don't recognise your account. Sign in to the control panel "
                "with the same email as your Slack account.")

    from app.auth import get_goal_role
    if await get_goal_role(user_uid, ctx.goal_id) is None:
        return "You don't have access to this goal."

    # `/run` and `/run <id>` are both status; the subcommands are a fixed set.
    if sub not in ("retry", "cancel", "more", "pause", "resume"):
        rest, sub = (args or "").strip(), "status"

    story_id, err = await story_for(ctx, rest if sub != "status" else rest, pool)
    if err:
        return err

    if sub == "status":
        return await _status(pool, ctx.goal_id, story_id)
    if sub == "retry":
        return await _retry(pool, ctx.goal_id, story_id, user_uid)

    # THE TEXT IS OPTIONAL AND THE BOX IS HOW YOU GIVE IT, which is what
    # the panel does: pause and cancel record an optional reason, and a
    # resume's text is a STEER the executor reads before its next model
    # call. Typing it inline still works -- and has to, because
    # `trigger_id` is perishable and absent on a Slack retry.
    # THE STORY'S OWN THREAD, so an explicit id works from any channel
    # in the goal and not only from the story's own.
    act_thread = await _thread_for_story(pool, story_id, ctx.thread_id)

    if sub in ("pause", "resume", "cancel") and not rest.strip():
        from app.channels.slack.run_modals import open_run_modal
        if await open_run_modal(pool, team_id=team_id, trigger_id=trigger_id,
                                action=sub, thread_id=act_thread,
                                goal_id=ctx.goal_id, channel_id=channel_id):
            return ""
        # No modal, so act on what we have. Pause and cancel are
        # meaningful with no reason; only `more` actually needs words.

    from app.services.kernel_more_work import more_work
    from app.services.kernel_run_control import cancel, pause, resume
    if sub == "pause":
        return await _run_control(pause, act_thread, ctx.goal_id,
                                  user_uid, rest, "Pausing at the next step "
                                  "boundary.")
    if sub == "resume":
        return await _run_control(resume, act_thread, ctx.goal_id,
                                  user_uid, rest, "Resumed.")
    if sub == "cancel":
        return await _run_control(cancel, act_thread, ctx.goal_id,
                                  user_uid, rest, "Cancelled.")
    if not rest.strip():
        return "Say what else needs doing: `/run more <what>`."
    return await _run_control(more_work, act_thread, ctx.goal_id,
                              user_uid, rest, "Queued.")


async def _status(pool, goal_id: str, story_id: str) -> str:
    row = await pool.fetchrow(
        """
        SELECT s.title, s.state, s.current_phase, e.id AS execution_id,
               e.state AS exec_state, e.phase, e.stall_reason
          FROM stories s
          LEFT JOIN kernel.executions e ON e.story_id = s.id
         WHERE s.id = $1
         ORDER BY e.id DESC NULLS LAST
         LIMIT 1
        """, story_id)
    if row is None:
        return f"No story `{story_id}`."

    lines = [f"*{row['title']}* (`{story_id[:8]}`) — {row['state']}"]
    if row["execution_id"]:
        # No leg counter here: "leg 13/14" is the assistant's own prose in
        # a thread, not a column. kernel.executions has state, phase and a
        # stall reason, and inventing a number from nothing would be worse
        # than showing what is actually recorded.
        detail = f"Execution {row['execution_id']}: {row['exec_state']}"
        if row["phase"]:
            detail += f" ({row['phase']})"
        lines.append(detail)
        if row["stall_reason"]:
            lines.append(f"Stalled: {row['stall_reason']}")
    else:
        lines.append("No execution has started.")

    parked = await pool.fetch(
        """
        SELECT q.id, q.kind, q.question_text
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
         WHERE e.story_id = $1 AND q.status = 'OPEN'
         ORDER BY q.id
        """, story_id)
    if parked:
        lines.append("")
        lines.append(f"*Waiting on you ({len(parked)}):*")
        for q in parked[:5]:
            text = (q["question_text"] or "").strip().splitlines()[:1]
            lines.append(f"  • `{q['id']}` {q['kind']}: {text[0] if text else ''}")
        lines.append("Answer with `/park answer <your answer>`.")
    return "\n".join(lines)


async def _retry(pool, goal_id: str, story_id: str, user_uid: str) -> str:
    eid = await pool.fetchval(
        "SELECT id FROM kernel.executions WHERE story_id = $1 "
        " ORDER BY id DESC LIMIT 1", story_id)
    if not eid:
        return "There's no execution to retry."
    try:
        from kernel.harness.retry import retry_story
        async with pool.acquire() as conn:
            out = await retry_story(conn, goal_id, int(eid), user_uid)
    except Exception as exc:                                   # noqa: BLE001
        return f"Couldn't retry: {exc}"
    cleared = out.get("cleared") if isinstance(out, dict) else None
    return f"Retrying execution {eid}." + (f" Cleared: {cleared}." if cleared else "")


async def _thread_for_story(pool, story_id: str, fallback: str) -> str:
    """The thread the run controls should act on.

    THE STORY'S THREAD, NOT THE CHANNEL'S. These controls are
    thread-keyed, and passing the channel's thread meant they only
    worked from inside a story's own channel: `/run cancel 3a1095ea`
    typed in the goal channel resolved the STORY correctly and then
    acted on the GOAL thread, which owns no run -- so it answered
    "nothing is running" about a story that was.

    Reading the story's own `active_thread_id` makes the id mean what
    it says everywhere. The channel stays the fallback, which is the
    common case and the one that needs no id at all.
    """
    if story_id:
        own = await pool.fetchval(
            "SELECT active_thread_id FROM stories WHERE id = $1", story_id)
        if own:
            return own
    return fallback


async def _run_control(action, thread_id: str, goal_id: str, user_uid: str,
                       text: str, done: str) -> str:
    """pause / cancel / more_work all take (thread_id, goal_id, actor_uid).

    The same functions the panel's buttons call, so the kill switch and
    the state machine cannot be bypassed by coming in through Slack.
    """
    if not thread_id:
        return ("That story has no open thread, so there's no run to act "
                "on. `/run <id>` will say what state it is in.")
    try:
        out = await action(thread_id, goal_id, user_uid, text)
    except Exception as exc:                                   # noqa: BLE001
        return f"Couldn't do that: {exc}"
    if isinstance(out, dict) and out.get("error"):
        return f"Refused: {out['error']}"
    return done
