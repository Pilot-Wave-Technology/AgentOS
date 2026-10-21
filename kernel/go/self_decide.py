"""May the GO decide this plan ALONE, without asking anybody?

WHAT CHANGED AND WHY IT IS WRITTEN DOWN. `decide_plan` was built to
RELAY: the GO asks a person, is answered, and carries the answer with
their uid on it. Its docstring argues that case well, and the argument
still holds for everything except the one shape this module admits --
an AUTONOMOUS errand, where the person approved the errand precisely so
they would not have to watch it, and where "ask a person and wait" puts
back the stall the verb was written to remove. Asked for deliberately
on 2026-09-24.

WHAT IS GIVEN UP, stated plainly rather than discovered later: a plan
can now authorise an executor to act unattended without any person
having seen it. That is a real reduction in oversight and the reason
every guard below exists.

WHAT STILL HOLDS, and none of it is new code:

    go_may_drive      an AUTONOMOUS errand with a live, unrevoked grant
                      -- enforced at dispatch for this verb already, so
                      a story and a hand-started errand cannot reach here
    the grant         a person approved THIS errand, which is the consent
                      being spent; revoking it stops this immediately
    acceptance        done-or-more-work is still a person's, untouched

WHAT IS NEW: a ceiling on how many plan cycles one run may self-approve.

WHY A CEILING AND NOT A CLEVERER RULE -- the same reasoning `go/answers`
gives for its cap, and it applies harder here. There is no reliable way
to tell "the third honest re-plan" from "the third turn of a loop" by
reading the plans. An executor and a GO can re-plan at each other
indefinitely, each approval perfectly reasonable in isolation, and the
run burns a leg a cycle with nobody watching. A number is crude, and
crude is what can be reasoned about when it fires.

PAST THE CEILING IS NOT A FAILURE. The run stays parked exactly where it
was, the question stays OPEN, and its card is already in the thread. The
ceiling does not create an interruption; it stops the GO removing one,
which is the right way round when nobody has looked at this work yet.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Plan cycles one run may self-approve before a person must look.
#
# THREE, and it is lower than `answers.CAP` on purpose. That cap bounds
# clarifications -- cheap, and answering one wrongly costs a wrong
# sentence. This bounds APPROVALS, where each one authorises unattended
# work, so the number that is "crude but arguable" is smaller.
CAP = 3

# Plan cycles already settled on this run. Counted from `questions`
# rather than from a new column: an authorization question that reached
# ANSWERED is exactly one cycle that was approved or sent back, which is
# the thing being bounded.
_CYCLES = """
    SELECT count(*) FROM kernel.questions
     WHERE execution_id = $1 AND kind = 'authorization'
       AND status = 'ANSWERED'
"""

# The grant, re-read HERE and not taken on trust from the caller. This
# is the consent being spent, and the whole point of reading it at the
# moment of use is that a person may have revoked it since the run
# started. Clause-for-clause `kernel/go/drivable._HAS_GRANT`.
_GRANT = """
    SELECT 1
      FROM kernel.errand_schedules s
      JOIN kernel.executions e ON e.goal_id = s.goal_id
     WHERE e.id = $1
       AND s.approved_by IS NOT NULL
       AND s.revoked_at IS NULL
       AND (s.expires_at IS NULL OR s.expires_at > now())
       AND (s.story_id IS NULL OR s.story_id = e.story_id)
     LIMIT 1
"""


async def alone_ok(conn, execution_id: int) -> str:
    """"" when the GO may decide this run's plan alone, else why not.

    A STRING AND NOT A BOOLEAN, because the caller has to tell a person
    what happened and "False" is not something anybody can act on.
    """
    if not await conn.fetchval(_GRANT, execution_id):
        return ("this errand has no live grant, so nobody has agreed to it "
                "running unattended. Ask a person and relay their answer "
                "with `human_uid`.")
    used = int(await conn.fetchval(_CYCLES, execution_id) or 0)
    if used >= CAP:
        logger.warning("execution %s: GO has settled %s plan cycles alone "
                       "and is capped; a person must look", execution_id, used)
        return (f"you have already settled {used} plan cycles on this run "
                f"alone, which is the ceiling. Re-planning this many times "
                f"is the shape of a loop rather than of progress, and "
                f"nobody has looked at this work yet. Say so on the thread "
                f"and let a person read it -- the run stays parked where it "
                f"is and its card is already there.")
    return ""
