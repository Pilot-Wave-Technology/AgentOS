"""Try again, because the thing that was in the way has been fixed.

NARROW ON PURPOSE, and the narrowness is the design. This clears the
counters and the recorded errors that stop a run being picked up again.
It does NOT change state, force a verdict, approve anything, or push a
run past a gate -- every loop that would act on this run already knows
how; what it needed was permission to stop giving up.

WHY IT IS NOT MOST OF WHAT YOU'D EXPECT. A run PARKED on a question
already has a way back: answering the question un-parks it and starts a
new leg (`resolve_request`). So "retry a parked run" is not this
function's job and adding it here would be a second resume path -- the
thing park.py's own docstring says must not exist. This is for the runs
that are stalled and NOT parked, and for the git engine, which records
`last_error` and then never tries again.

WHY A HUMAN UID IS REQUIRED. The counters this resets exist to stop
loops: `verify_error_count` is what finally ended an adjudicator that
retried a non-replayable write for ever. An assistant that could reset
them whenever it noticed a stall would rebuild that loop exactly. A
person saying "I re-minted the token, go" is a judgement about the
world outside the kernel, which is the one thing an agent cannot check
and a person can.
"""

from __future__ import annotations

import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)


async def retry_story(conn, goal_id: str, execution_id: int,
                      human_uid: str, note: str = "") -> dict:
    """Clear what is holding this run back. Returns what it cleared."""
    if not (human_uid or "").strip():
        # H8a's rule, for the same reason: this is a claim about the
        # world outside the kernel, and only a person can make one.
        return Refused(
            "E_NOT_HUMAN",
            "a retry asserts that the cause has been fixed, which is a "
            "judgement about the world outside this system; it needs a "
            "person's identity")

    row = await conn.fetchrow(
        "SELECT id, goal_id, story_id, state, stall_reason, "
        "verify_error_count, publish_error_count "
        "FROM executions WHERE id = $1", execution_id)
    if row is None:
        return Refused("E_NO_SUCH_RUN", f"no run {execution_id}")
    if goal_id and row["goal_id"] != goal_id:
        return Refused("E_NOT_IN_GOAL",
                       f"run {execution_id} belongs to another goal")
    if row["state"] in ("DONE", "CANCELLED", "FAILED", "ABANDONED"):
        return Refused(
            "E_TERMINAL",
            f"run {execution_id} is {row['state']} and has nothing left to "
            f"retry; start the story again if there is more to do")

    open_q = await conn.fetchval(
        "SELECT kind FROM questions WHERE execution_id = $1 "
        "AND status = 'OPEN' LIMIT 1", execution_id)
    if open_q:
        # The honest refusal. Clearing the counters under a parked run
        # would look like it worked and change nothing -- the run is
        # waiting on the question, not on the counters.
        return Refused(
            "E_WAITING_ON_YOU",
            f"this run is parked on a {open_q} question, not stalled. "
            f"Answering it is what resumes the run; a retry would clear "
            f"counters nothing is currently reading")

    cleared: list = []
    if row["stall_reason"]:
        cleared.append(f"stall: {row['stall_reason'][:120]}")
    if (row["verify_error_count"] or 0) > 0:
        cleared.append(f"{row['verify_error_count']} verification strike(s)")
    # THE PUBLISH STREAK TOO. Without this, "I granted the permission"
    # clears the stall and the run parks again on its very next
    # refusal, because the count that decided to park is still sitting
    # at its threshold. Retry is the gesture that says the outside
    # world changed; the counter is a claim about the outside world.
    if (row["publish_error_count"] or 0) > 0:
        cleared.append(f"{row['publish_error_count']} publish refusal(s)")
    await conn.execute(
        "UPDATE executions SET stall_reason = NULL, verify_error_count = 0, "
        "publish_error_count = 0, updated_at = now() WHERE id = $1",
        execution_id)

    # THE ENGINE TOO. `record_node_github_failure` writes last_error and
    # nothing retries it, so a story whose push was refused stays refused
    # even after the permission is granted. Clearing it here is what
    # makes "I fixed the token" actually mean something.
    nodes = await conn.fetch(
        """
        UPDATE git_nodes SET last_error = NULL, error_card_error = NULL,
                             updated_at = now()
         WHERE last_error IS NOT NULL
           AND id IN (SELECT git_node_id FROM git_node_stories
                       WHERE story_id = $1)
        RETURNING branch_name
        """, row["story_id"] or "")
    for n in nodes:
        cleared.append(f"git error on {n['branch_name']}")

    logger.info("run %s: retry by %s (%s); cleared %s", execution_id,
                human_uid, note or "no note", cleared or "nothing")
    if not cleared:
        return {"run": execution_id, "story": row["story_id"],
                "cleared": [], "state": row["state"],
                "detail": "nothing was holding this run back; it is "
                          f"{row['state']} and the loops already have it"}
    return {"run": execution_id, "story": row["story_id"],
            "cleared": cleared, "state": row["state"],
            "detail": "cleared; the loops pick this up on their next pass. "
                      "Nothing else changed -- no state was forced and no "
                      "gate was passed"}
