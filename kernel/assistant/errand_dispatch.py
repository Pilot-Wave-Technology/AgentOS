"""Dispatch another run of an errand somebody already approved.

NOTHING SCHEDULES AN ERRAND. There is no cron and no timer: the first
run goes out when a person approves the errand, and every run after that
is one somebody asked for. An autonomous errand is not a recurring one --
"autonomous" says a run finishes without anybody accepting it, and says
nothing about when runs happen.

WHY THIS IS NEEDED AT ALL. A run can end without producing the work:
cancelled, failed, or -- run 345 -- killed while its pod was still being
scheduled. The errand is still approved and still wanted, so somebody
says "dispatch it again". Before this there was no way to: the schedule
had been retired by its own first run, and `run_control` answered
`E_NO_RUN` because nothing was in flight to control.

A RELAY, NOT A JUDGMENT. `human_uid` is required for the same reason it
is required to cancel: a run costs the approver's budget and carries
their name, and an agent that may start runs on its own reading of what
is wanted is an agent that spends money nobody asked it to. Only the
FIRST run is automatic, because a person approving the errand IS the
decision to run it once. Every one after that needs somebody to say so.

HOW IT DISPATCHES, and why not directly. It sets the grant's next run
time and stops. The sweep is the only thing that creates an errand's
execution, so going around it would be a second way to start a run --
with its own admission, its own kill switch, its own bugs. Setting the
time puts the request in front of machinery that already works.
"""
from __future__ import annotations

import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

# The grant must be real, live and unrevoked -- the same predicate the
# check-in watch and `standing_approval` use. Re-running is running.
_ASK = """
    UPDATE kernel.errand_schedules
       SET next_run_at = now(), paused = false, retired_reason = NULL
     WHERE story_id = $1
       AND approved_by IS NOT NULL
       AND revoked_at IS NULL
       AND (expires_at IS NULL OR expires_at > now())
 RETURNING id
"""

_LIVE_RUN = """
    SELECT x.id, x.state
      FROM kernel.executions x
      JOIN kernel.run_state_machine m ON m.version = x.machine_version
     WHERE x.story_id = $1
       AND NOT (m.terminal_states @> to_jsonb(x.state))
     ORDER BY x.id DESC LIMIT 1
"""


async def dispatch_errand(conn, goal_id: str, story_id: str,
                          human_uid: str, actor_id: str = ""
                          ) -> dict | Refused:
    """Queue one more run of this errand, carrying a person's decision."""
    story_id = (story_id or "").strip()
    if not story_id:
        return Refused(
            "E_NO_STORY",
            "say which errand to run again, by story id. `goal_state` "
            "lists them.")

    if not (human_uid or "").strip():
        return Refused(
            "E_NO_HUMAN",
            "a run spends the approver's budget and carries their name, "
            "so starting one is somebody's decision to make. Relay it "
            "with their uid. Only the errand's FIRST run is automatic -- "
            "approving it was that decision. If nobody has asked for "
            "another, say on the thread that it can be run again and let "
            "them decide.")

    row = await conn.fetchrow(
        "SELECT state, work_kind, title FROM stories "
        " WHERE id = $1 AND goal_id = $2", story_id, goal_id)
    if row is None:
        return Refused("E_NO_SUCH_STORY",
                       f"{story_id} is not a story in this goal")

    # READ FROM THE ROW, never from the caller -- the same rule
    # `accept_errand` follows. An ordinary story is started by the person
    # it belongs to, from the board.
    if (row["work_kind"] or "story") != "errand":
        return Refused(
            "E_NOT_AN_ERRAND",
            f"{story_id} is an ordinary story, not an errand. Ordinary "
            f"stories are started by the person they are assigned to.")

    # ALREADY RUNNING IS AN ANSWER, NOT A FAILURE. The request would be
    # queued correctly -- `due` will not claim a schedule whose story has
    # a live run -- but queueing silently is how somebody ends up with a
    # second run they did not mean to ask for. Say what is happening and
    # let them ask again when it matters.
    live = await conn.fetchrow(_LIVE_RUN, story_id)
    if live is not None:
        return Refused(
            "E_ALREADY_RUNNING",
            f"run {live['id']} of that errand is {live['state'].lower()}, "
            f"so it is already going -- an errand runs one at a time. If "
            f"it is stuck, that is `run_control`; if they want another "
            f"run AFTER this one, ask again when this one finishes.")

    sched = await conn.fetchval(_ASK, story_id)
    if sched is None:
        return Refused(
            "E_NO_GRANT",
            "that errand has no live grant to run under -- it was never "
            "approved, or the grant has been revoked or has expired. "
            "Propose it again for somebody to approve.")

    logger.info("errand dispatch: story=%s schedule=%s by %s for %s",
                story_id, sched, actor_id or "human", human_uid)
    return {"story": story_id, "title": row["title"],
            "requested_by": human_uid,
            "detail": "queued -- it starts as soon as a slot is free. Its "
                      "plan is re-approved automatically only if it is "
                      "the same plan that person approved before; any "
                      "other plan parks for them."}
