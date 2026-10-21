"""I5h — how many questions GO may settle on one run before a person must.

THE RISK THIS BOUNDS, in the words of the design's own risk section: *GO
answering its own errand's questions in a loop.* The existing limits do
not touch it. Three distinct attempts before a `clarification` is
allowed, one open question per execution, content-hash dedupe and
`max_legs` all bound how often the SAME question can be asked; none of
them bounds how many DIFFERENT ones an agent may answer for another
agent, which is the shape a two-model loop actually takes.

WHY A CAP AND NOT A CLEVERER RULE. There is no reliable way to tell "the
fourth genuine clarification" from "the fourth turn of a loop" by looking
at the questions. A number is crude, and crude is what can be reasoned
about at three in the morning when a run has been going for an hour.

WHAT TRIPPING IT COSTS: nothing is lost and nothing is stranded. The run
stays parked exactly where it was, the question stays OPEN, and its card
is already in the driving human's thread — the card poller posts one for
every open question whatever its kind. So the cap does not send a person
a new interruption; it stops GO answering and leaves the interruption
they already had.

COUNTED FROM `proposals`, not from `questions.answered_by`. That column
is a free string an agent fills in with its own name, which is exactly
how the approval hole stayed open (H8a). A proposal row carries the
actor the mailbox recorded.

PER RUN, NOT PER GOAL OR PER DAY. The loop is inside one execution: a
goal with twenty healthy errands should not run out of answers because
one of them was chatty last week.
"""

from __future__ import annotations

import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

# Enough for a real errand to be genuinely unclear a few times; low
# enough that a loop trips it in well under a minute of wall clock.
#
# RAISED FROM 3 WHEN GO STARTED DRIVING. The old number came with a
# justification that has since inverted: "an errand that needs a fourth
# clarification from its own PLANNER is one whose task text was wrong".
# That was true of a GO which planned an errand, handed it over and went
# away -- three questions from something not watching really was a
# signal the brief was bad.
#
# GO now holds a thread per live errand, reads its progress, and is the
# agent accountable for it while it runs. A long errand asking six
# things over several hours is a working relationship, not a broken
# brief, and hitting the cap at three parked the errand on a person for
# something GO could answer in a sentence -- which is the exact cost
# I5e existed to remove.
#
# THE LOOP GUARD IS WHAT THE CAP IS FOR, and it survives: an executor
# and GO ping-ponging burn ten in seconds, so this still trips fast. The
# number is a circuit breaker, not a budget, and the escalation at the
# cap is the designed behaviour rather than a failure -- past here a
# person reads the errand, which is right.
CAP = 10


async def answered_by_go(conn, execution_id: int, actor: str) -> int:
    """How many of this run's questions this agent has already settled."""
    return int(await conn.fetchval(
        """
        SELECT count(*) FROM kernel.proposals
         WHERE execution_id = $1 AND kind = 'answer' AND actor_id = $2
        """, execution_id, actor) or 0)


async def may_answer(conn, question_id: int, actor: str):
    """None if this agent may settle that question, else a Refused.

    Takes a QUESTION and finds the run, rather than taking a run, so the
    caller cannot pass the wrong one — the tool's argument is a question
    id and nothing else.
    """
    row = await conn.fetchrow(
        "SELECT q.execution_id, e.kind FROM questions q "
        "JOIN executions e ON e.id = q.execution_id WHERE q.id = $1",
        question_id)
    if row is not None and row["kind"] != "errand":
        # F5 — STORIES ARE NOT GO'S TO ANSWER.
        #
        # I5e gave GO `relay_answer` so it could unblock an ERRAND it
        # planned, and the wake that tells it about parks is filtered to
        # errands in three places. The TOOL was filtered in none, so GO
        # could settle a clarification on a story too -- and did, live,
        # on story 36443e64.
        #
        # Harmless that time: a clarification cannot approve anything and
        # GO's answer was right. But a story already has an assistant
        # whose entire job is that story, so two agents could answer one
        # question with nothing coordinating them, and whichever got
        # there first would win. The brief for this stage was that story
        # executions are not affected, and this was an effect.
        #
        # Scoped to match the wake rather than widening the wake to match
        # the tool: widening later is easy, un-shipping a capability
        # people have started relying on is not.
        return Refused(
            "E_NOT_YOUR_RUN",
            f"question {question_id} is on a STORY, and a story has an "
            f"assistant whose job is exactly that. Two agents answering "
            f"one question is how a person gets told something was "
            f"settled that was not. If the answer matters and you have "
            f"it, say so in the goal thread -- the assistant reads it and "
            f"relays it, which also leaves the person a record of where "
            f"it came from.")
    execution_id = row["execution_id"] if row else None
    if execution_id is None:
        # Not this function's refusal to make. `relay_answer` says
        # E_NO_QUESTION with the id in it, and two modules answering the
        # same way about a missing row is two things to keep in step.
        return None

    used = await answered_by_go(conn, execution_id, actor)
    if used < CAP:
        return None

    logger.warning("execution %s: %s has answered %s questions and is "
                   "capped; the run stays parked for a person",
                   execution_id, actor, used)
    return Refused(
        "E_ANSWER_CAP",
        f"you have answered {used} questions on run {execution_id}, which "
        f"is the limit for one run. An errand that needs a {used + 1}th "
        f"clarification from the agent that planned it is one whose task "
        f"text was wrong, and answering again is how two models talk to "
        f"each other for an hour.\n\n"
        f"THE RUN IS NOT LOST and nothing has failed — it stays parked "
        f"exactly where it is, and a card for this question is already in "
        f"the thread of the person driving it. Say in the goal thread what "
        f"it keeps asking about and what you think the task text should "
        f"have said. That is the useful thing you have that they do not.")
