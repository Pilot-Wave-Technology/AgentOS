"""One shape for every errand wake.

WHY THEY NEEDED A SHAPE. Three wakes had three of them: the park opened
with an authority ruling, the check-in with a status line, the answer
with a transcript. GO had to work out what kind of thing it was holding
before it could act on it, on every wake, under load.

EVIDENCE BELONGS IN THE WAKE; PROCEDURE BELONGS IN THE PROMPT. The old
wakes carried both, so the four-step ask sequence was written twice --
once here and once in `AGENTS.md` -- and the two drifted, which is the
failure that cost runs 307 and 308. It also made every wake about twenty
lines, and a check-in adds its whole text to GO's context permanently:
at four an hour on a long errand that is real money for a sentence GO
already knows.

So the wake carries what GO cannot get without a tool call -- the plan,
the executor's answer, the stall note -- and ONE imperative naming the
tool. How to do it is on the page GO reads once a session.

AND EVERY WAKE NAMES A PRINCIPAL. Not by pretending to be them: a wake
written as if the person had typed it would have GO answering someone
who is not there, treating a scheduled check as an interrupt, and
finding out it was lied to the first time it looked. The honest frame is
stronger anyway -- a named person delegated a standing job and went
away. That is what makes "report back" have a recipient, and reporting
was the first thing GO dropped when it had none (run 308: a plan
approved in silence).
"""

from __future__ import annotations


def wake(header: str, story: str, title: str, approver: str,
         happened: str, evidence: str, imperative: str) -> str:
    """Assemble one wake.

    THE ORDER IS THE POINT. What it is, who it is for, what happened,
    what is known, what to do -- so GO can stop reading as soon as it
    has what it needs, and so two wakes never disagree about where a
    fact lives.
    """
    # TWO SENTENCES, because one with a name slotted in does not read
    # when the name is missing: "For the person who approved this
    # errand, who approved this errand to run without them."
    named = approver.strip()
    who = named or "the person who approved this errand"
    line = (f"For {named}, who approved this errand to run without them."
            if named else
            "For the person who approved this errand to run without them.")
    parts = [f"[{header}] {story} - \"{title}\"",
             line,
             "",
             happened.strip()]
    if evidence.strip():
        parts += ["", evidence.strip()]
    # ITS OWN PARAGRAPH. Appended to the imperative it produced "Then
    # leave it. Then report back...", and an imperative that ends in a
    # full stop should not have a sentence glued to it.
    parts += ["", f"-> {imperative.strip()}",
              "", f"Then report back to {who} on this thread. One line is "
                  f"enough when nothing is wrong; silence is not a report."]
    # JOINED ON ONE NEWLINE with "" entries as the blank lines. An
    # earlier version then collapsed doubles, which removed every
    # paragraph break it had just made.
    return "\n".join(parts)


# THE APPROVER'S NAME, and it has to be a name. `approved_by` is a
# firebase uid -- "rPeDJCGuzKeka9yyYARENy4rhT22" -- and a wake addressed
# to that is worse than one addressed to nobody: it reads as machine
# plumbing, which is exactly the tone the principal is there to
# displace.
_APPROVER = """
    SELECT COALESCE(NULLIF(u.display_name, ''), NULLIF(u.email, ''), '')
      FROM kernel.errand_schedules s
      LEFT JOIN users u ON u.uid = s.approved_by
     WHERE s.goal_id = $1 AND s.approved_by IS NOT NULL
       AND s.revoked_at IS NULL
       AND (s.story_id IS NULL OR s.story_id = $2)
     ORDER BY (s.story_id IS NOT NULL) DESC
     LIMIT 1
"""


async def approver(conn, goal_id: str, story_id: str) -> str:
    """Who approved this errand, by name. "" when it cannot be read.

    FALLS BACK TO A PHRASE, not to a blank: `wake()` turns "" into "the
    person who approved this errand", which is true, readable, and
    keeps the shape intact. A wake must never fail because a display
    name is missing.
    """
    try:
        return (await conn.fetchval(_APPROVER, goal_id, story_id)) or ""
    except Exception:                                       # noqa: BLE001
        return ""
