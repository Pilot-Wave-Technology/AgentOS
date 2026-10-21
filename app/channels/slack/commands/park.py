"""`/park` — answer the question an execution is waiting on.

THE MOST EXPENSIVE SILENCE IN THE PRODUCT. A parked execution is work
that has stopped and will not restart until a person says something,
and until now the only place to say it was a browser. An assistant
parked at 11pm on "may I run this irreversible command" waited until
somebody opened the panel.

The card in Slack now has buttons for it (phase 8), but a card
scrolls away and a question with no buttons -- a clarification, a
direction -- never had any. `/park` is the surface that does not
depend on finding the right message.

Calls `kernel_decide.decide`, the same function the panel calls. It
carries the verdict vocabulary, the check rewriting and the rule that
an approval must name a human; a second implementation would carry
none of that.
"""

from __future__ import annotations

import logging

from app.channels.slack.commands._context import resolve, story_for

_log = logging.getLogger(__name__)

USAGE = (
    "/park — the question this story is waiting on\n"
    "\n"
    "  /park                       show the open question(s)\n"
    "  /park answer <text>         answer it\n"
    "  /park approve               approve the plan as it stands\n"
    "  /park revise <what>         send it back with changes\n"
    "  /park decline <why>         refuse it\n"
    "\n"
    "In a story's own channel no id is needed. Elsewhere: "
    "`/park answer <story-id> <text>`."
)

# The card's buttons and these words land on the same verdicts.
_VERDICTS = {"approve": "approve", "approved": "approve",
             "revise": "revise", "decline": "declined",
             "declined": "declined", "answer": "answer"}


async def handle(args: str, channel_id: str, team_id: str, *,
                 slack_user_id: str = "", response_url: str = "",
                 command: str = "park") -> str:
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

    story_id, err = await story_for(ctx, "" if sub in _VERDICTS else args, pool)
    if err:
        return err

    open_qs = await pool.fetch(
        """
        SELECT q.id, q.kind, q.question_text
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
         WHERE e.story_id = $1 AND q.status = 'OPEN'
         ORDER BY q.id
        """, story_id)

    if not sub or sub not in _VERDICTS:
        return _render(open_qs, story_id)

    if not open_qs:
        return "Nothing is waiting on an answer for this story."
    if len(open_qs) > 1:
        return (f"{len(open_qs)} questions are open — answer them in the "
                f"panel, or on their cards in this channel, so the right one "
                f"gets the right answer.")

    question = open_qs[0]
    verdict = _VERDICTS[sub]
    text = rest.strip()

    if verdict == "answer":
        # A free-text answer to a clarification. The kernel calls that a
        # revise with the text attached: there is no separate verdict,
        # and inventing one here would mean two vocabularies.
        if not text:
            return "Say what the answer is: `/park answer <text>`."
        verdict = "revise"
    elif verdict in ("revise", "declined") and not text:
        return (f"A {sub} needs a reason, or the model restarts the same "
                f"plan: `/park {sub} <what should change>`.")

    display = await pool.fetchval(
        "SELECT display_name FROM users WHERE uid = $1", user_uid) or ""
    try:
        from app.services.kernel_decide import decide
        out = await decide(question_id=int(question["id"]), verdict=verdict,
                           text=text, human_uid=user_uid, display_name=display)
    except Exception as exc:                                   # noqa: BLE001
        return f"Couldn't record that: {exc}"
    return f"Recorded: {out.get('verdict', verdict)}. The run continues."


def _render(open_qs, story_id: str) -> str:
    if not open_qs:
        return f"Nothing is waiting on you for `{story_id[:8]}`."
    lines = [f"*Waiting on you ({len(open_qs)}):*"]
    for q in open_qs:
        text = (q["question_text"] or "").strip()
        first = text.splitlines()[0] if text else ""
        lines.append(f"  • `{q['id']}` *{q['kind']}* — {first}")
    lines.append("")
    lines.append("Answer with `/park answer <text>`, or use the buttons on "
                 "the card above.")
    return "\n".join(lines)
