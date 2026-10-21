"""Close a cancelled story's conversations, here and on the gateway.

WHAT WAS LEFT BEHIND. Cancelling stopped the run, bridged the
dependents and retired the branch, and left the story's thread sitting
open in the goal's list with an agent session behind it. For an errand
that thread is where GO drove the work: it stays in the sidebar looking
live, it still has a session the gateway will happily resume, and a
person clicking it gets a conversation about work that no longer exists.

TWO HALVES, AND BOTH ARE NEEDED. Closing the row hides it from the UI
(`goals.py` lists `status IN ('active','open')`) but leaves the session
on the gateway, holding a context window and resumable by anything that
still knows the key. Deleting the session without closing the row leaves
a thread in the list that reopens empty. So: session first, row second.

NEVER RAISES, ON THE SAME REASONING AS `stop_the_run`. The story is
already cancelled by the time this runs. A gateway that is down, or a
session that was already gone, must not turn a completed cancellation
into a failed request -- the thread stays open, which is untidy, and the
log says so.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# Threads the UI shows for this story. `threads.story_id` covers both an
# errand's driving thread and an ordinary story's conversation, so there
# is no second lookup against `errand_schedules`.
_THREADS = """
    SELECT id, instance_id, agent_id
      FROM threads
     WHERE story_id = $1 AND goal_id = $2
       AND (status IS NULL OR status = '' OR status IN ('active', 'open'))
"""


async def close_story_sessions(conn, goal_id: str, story_id: str) -> int:
    """Close this story's threads and delete their gateway sessions.

    Returns how many threads were closed.
    """
    try:
        from app.openclaw import gateway_admin, make_goal_session_key
    except Exception:                                       # noqa: BLE001
        _log.exception("session teardown %s: gateway import failed", story_id)
        return 0

    # ITS OWN SQL IS GUARDED TOO, and the docstring above was a promise
    # this function did not keep. Only the gateway call was wrapped, so
    # when the cancellation's transaction was already aborted by an
    # earlier failure, this fetch raised `InFailedSQLTransactionError`
    # and 500'd a cancellation that had otherwise succeeded -- turning
    # somebody else's silent bug into a visibly broken button.
    #
    # An unreachable database leaves a thread open. That is untidy and
    # it is not worth failing a completed cancellation for.
    try:
        rows = await conn.fetch(_THREADS, story_id, goal_id)
    except Exception:                                       # noqa: BLE001
        _log.exception("session teardown %s: could not read its threads",
                       story_id)
        return 0

    closed = 0
    for row in rows:
        # THE INSTANCE, NOT THE TEMPLATE. The session key is built from
        # the agent instance that actually held the conversation;
        # `agent_id` is the fallback for older rows written before
        # `instance_id` was populated, and a key built from the wrong one
        # deletes nothing and reports success.
        who = row["instance_id"] or row["agent_id"]
        if who:
            try:
                await gateway_admin.delete_session(
                    make_goal_session_key(who, goal_id, row["id"]),
                    delete_transcript=True)
            except Exception as exc:                        # noqa: BLE001
                # ALREADY GONE IS SUCCESS, and it is the common case: a
                # goal whose gateway agent was rebuilt has no session
                # under the old key. Logged at info so a real outage is
                # still visible in the warning stream.
                _log.info("session teardown %s: gateway said %s",
                          story_id, exc)
        try:
            await conn.execute(
                "UPDATE threads SET status = 'closed' WHERE id = $1",
                row["id"])
        except Exception:                                   # noqa: BLE001
            _log.exception("session teardown %s: could not close thread %s",
                           story_id, row["id"])
            continue
        closed += 1

    if closed:
        _log.info("session teardown %s: closed %d thread(s)",
                  story_id, closed)
    return closed
