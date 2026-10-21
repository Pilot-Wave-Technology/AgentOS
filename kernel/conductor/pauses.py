"""Does the kill switch say stop? The kernel's half of it.

WHAT WAS WRONG. `execution_pauses` is the single source of truth for "is
execution paused?" and every dispatch path in `app/` consults it. The
string appeared NOWHERE under `kernel/`. So pressing Freeze System
turned the banner red, stopped control-panel dispatch and stopped
conversational replies -- while every admitted kernel execution kept
taking legs, spending tokens, running model-authored bash and committing
to git. The one lever an operator reaches for first was the one that did
not reach the thing writing to git.

The comment at `kernel/worker/loop.py` says "admission, the kill switch
and every ceiling then apply". The first and third were true.

TWO GATES, because one is not enough:

  admission   no NEW work starts. Necessary, and on its own it only
              stops the queue growing -- everything already admitted
              runs to completion, which at a leg apiece is hours.
  the leg     a RUNNING execution does not take its next one. This is
              the gate that actually stops the bleeding, and it stops it
              at a boundary the system already treats as safe: between
              legs, with the workspace committed and the lease released.

NOT mid-leg. A leg is a model turn and its tool calls; interrupting one
leaves a sandbox in a state nobody described, and the existing machinery
deliberately has no cancellation for exactly that reason. Freeze means
"finish what you are holding and take nothing new", not "stop dead".

FAIL CLOSED, matching the direction `app/services/execution_control.py`
chose for `dispatch_block` and giving the same reason: on an unexpected
error report a block, so work safely waits a tick rather than
dispatching during a freeze nobody can see. A tick is four seconds; the
alternative is an agent writing to a repository while an operator
believes it is stopped.

READ ONLY. A worker may ask whether it is allowed to proceed; it may not
pause or resume. A run that could clear its own pause is not a kill
switch, it is a suggestion. The grant is SELECT and nothing else.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The system singleton uses scope_id = '' -- see migrations/
# execution_pauses.sql. `agent_instance` scope is deliberately NOT
# consulted here: it is a graceful stop for one control-panel agent
# instance and has no kernel equivalent, and treating it as a kernel
# freeze would stop unrelated stories.
#
# `story` SCOPE EXISTS IN THE SCHEMA AND IS DELIBERATELY NOT READ. A
# draft of the GO's errand controls wrote one, and it was the wrong
# mechanism: the Pause BUTTON submits a `hold` PROPOSAL, which the leg
# takes at a step boundary and `resume` answers. Two ways to pause one
# run meant "why is it not paused" had two answers. `kernel/go/
# run_control.py` now uses the proposal for both. The column widening
# in migrations/execution_pauses_story_scope.sql is harmless and was
# left rather than narrowed back.
SQL = """
    SELECT scope, scope_id, reason, paused_by
      FROM public.execution_pauses
     WHERE scope = 'system'
        OR (scope = 'goal' AND scope_id = $1)
     ORDER BY CASE scope WHEN 'system' THEN 0 ELSE 1 END
     LIMIT 1
"""


async def frozen(conn, goal_id: str | None = None) -> dict | None:
    """The pause that blocks this run, or None.

    `goal_id` of None means "every goal", which is how a standing worker
    runs -- so only a system freeze can block it. That is correct: a
    per-goal pause must not stop stories in other goals.

    THE WIDER PAUSE IS REPORTED FIRST. Both can match at once, and an
    operator told "goal KER12 is paused" during a SYSTEM freeze would go
    and un-freeze the goal and find nothing moved. `describe()` renders
    whichever row comes back, so the ORDER BY is the whole of that fix.
    """
    try:
        row = await conn.fetchrow(SQL, goal_id or "")
    except Exception:                                       # noqa: BLE001
        # FAIL CLOSED. An unreadable kill switch is not an absent one.
        logger.exception("kill switch unreadable; treating as FROZEN")
        return {"scope": "unknown", "scope_id": "",
                "reason": "the pause table could not be read",
                "paused_by": ""}
    if row is None:
        return None
    return dict(row)


def describe(pause: dict) -> str:
    """One line for a log or an action list."""
    scope = pause.get("scope") or "?"
    who = pause.get("paused_by") or "someone"
    why = pause.get("reason") or "no reason given"
    where = f" ({pause['scope_id']})" if pause.get("scope_id") else ""
    return f"{scope}{where} frozen by {who}: {why}"
