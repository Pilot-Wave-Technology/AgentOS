"""Can this story's machine still be changed?

THE RULE IS "NO RUN IS IN FLIGHT", NOT A LIST OF STATES. It used to be
`state in ('proposed', 'accepted')`, copied from `story_executor_model`,
and that whitelist is wrong in the one case people actually hit: a run
starts on `small`, dies for want of memory, somebody cancels it and
wants to try `medium`. The story is no longer `accepted` by then, so the
form refused -- and the only way through was to cancel the story
outright and write a new one.

So: editable whenever nothing is running, locked while something is.
That covers `proposed` and `accepted` as before, and it covers the
cancel-and-retry the whitelist did not.

WHY LOCKED AT ALL. The rung is frozen onto the story at dispatch and the
pod is already built from it, so a later edit would change a setting the
running leg cannot honour -- a form that looks like it worked and
changes nothing, which is worse than a refusal because the next failure
looks identical to the last one.

THE FREEZE AND THIS AGREE BY CONSTRUCTION. `size_choice.freeze` writes
`WHERE sandbox_size IS NULL`, so it pins the first dispatch's choice and
never overwrites a later one. Change the machine after a cancel and the
next dispatch uses what you set.

READ-ONLY AND FAIL-CLOSED. An unreadable `executions` table reports
LOCKED: refusing an edit is recoverable, and letting one through while a
pod is running produces a setting nobody can explain.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# A run that has reached one of these is over and holds nothing.
TERMINAL = ("DONE", "FAILED", "CANCELLED")

_LIVE = """
    SELECT id, state FROM kernel.executions
     WHERE story_id = $1 AND state <> ALL($2::text[])
     ORDER BY id DESC LIMIT 1
"""


async def locked_reason(pool, story_id: str) -> str | None:
    """Why the machine cannot be changed now, or None if it can."""
    if not (story_id or "").strip():
        return None
    try:
        row = await pool.fetchrow(_LIVE, story_id, list(TERMINAL))
    except Exception:                                       # noqa: BLE001
        _log.warning("machine lock: could not read runs for %s; treating "
                     "it as locked", story_id, exc_info=True)
        return ("could not check whether a run is in flight, so the "
                "machine is left alone")
    if row is None:
        return None
    return (f"run {row['id']} is {row['state'].lower().replace('_', ' ')}. "
            f"The machine is fixed for the life of a run -- the pod is "
            f"already built from it. Cancel the run and change it then; "
            f"the next one will use what you set.")
