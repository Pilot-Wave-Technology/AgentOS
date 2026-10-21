"""Is this story already running?

WHY THIS EXISTS NOW. `executions_one_live_per_story` makes the database
refuse a second non-terminal execution for one story, which is the right
place for that rule: the case it has to stop is two writers in the same
moment, and there are two conductor replicas. But a constraint refuses by
raising, and a raise is a bad answer for both of the callers that can hit
it:

  * `start_story` is a TOOL. An unhandled `UniqueViolationError` reaches
    the assistant as a stack trace rather than a typed refusal, and the
    assistant's only available reading of that is "the platform broke" --
    when what actually happened is ordinary and worth saying plainly.
  * the errand scheduler sweeps every tick. A run still going when the
    next tick fires is NORMAL for any schedule whose period is shorter
    than its work, and logging an exception for it each time turns a
    healthy state into a recurring fault nobody can act on.

So the check is READ FIRST and the constraint is the backstop. That is
deliberate belt-and-braces, not redundancy: the read gives a good message
in the overwhelmingly common case, and the constraint is what holds when
two writers arrive together and both reads say "clear".

THE TERMINAL SET IS THE POD LOOP'S, imported rather than respelled. The
index predicate has to restate it in SQL because an index cannot call
Python; this does not, and a third copy would be a third thing to keep in
step.
"""

from __future__ import annotations

from kernel.worker.pod_loop import TERMINAL

_LIVE = """
    SELECT id, state, created_at
      FROM executions
     WHERE story_id = $1
       AND state <> ALL($2::text[])
     ORDER BY id DESC
     LIMIT 1
"""


async def live_run(conn, story_id: str) -> dict | None:
    """The story's un-finished execution, or None.

    None also for a story_id that is empty or absent — an execution with
    no story is unconstrained by design (console fixtures and probes make
    several), and the index has the same `story_id IS NOT NULL` guard.
    """
    if not (story_id or "").strip():
        return None
    row = await conn.fetchrow(_LIVE, story_id, list(TERMINAL))
    return dict(row) if row else None


def already_running(story_id: str, run: dict) -> str:
    """The sentence a person or an agent should read.

    Names the execution, because "it is already running" without saying
    WHICH leaves the reader with no next step. With the id they can look
    at it, steer it, or stop it — which are the three things anybody
    actually wants at this point.
    """
    return (f"story {story_id} is already running as execution "
            f"{run['id']} ({run['state']}). A story has one run at a time: "
            f"a second would take its own admission slot and work the same "
            f"branch in a second pod. Steer or stop that run, or wait for "
            f"it to finish and start a new one.")
