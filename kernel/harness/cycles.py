"""When one cycle of a run ends and the next begins. One definition.

A CYCLE ENDS WHEN ITS CLAIM IS ANSWERED. Two readers depend on that
and they used to say it separately:

  planfiles     which `plan-NN.md` a newly registered plan is written to
  park_approval whether the plan approved earlier is spent, so the next
                piece of work needs a plan of its own

Both counted ANSWERED `acceptance` questions (and verdicts). A story run
with STORY_HOLDS_FOR_MERGE never parks on an acceptance: it finishes,
pushes and parks on a `landing` question instead -- "merge its pull
request, or say what more it should do". So neither reader ever saw
that cycle end. On BMIF1 run 2 the trunk-integration plan, two revisions
of it and a conflict-resolution plan were all treated as rewordings of
the feature plan and each one overwrote `plan-01.md`: the record of what
was actually built was gone from the PR it was meant to ride.

An answered landing is a finished cycle for the same reason an answered
acceptance is: the work was claimed done and somebody (a person, or the
platform retiring it to integrate a moved trunk) said what happens next.
A landing answered by the MERGE ends the run outright, so counting it
can never misnumber a plan that follows.
"""
from __future__ import annotations

# The question kinds whose ANSWER closes the cycle that raised them.
CYCLE_ENDING_KINDS = ("acceptance", "landing")


async def finished_cycles(conn, execution_id: int) -> int:
    """How many cycles of this run have had their claim answered."""
    return int(await conn.fetchval(
        "SELECT count(*) FROM questions WHERE execution_id = $1 "
        "  AND kind = ANY($2::text[]) AND status = 'ANSWERED'",
        execution_id, list(CYCLE_ENDING_KINDS)) or 0)

