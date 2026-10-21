"""Which GO thread a request is asking for.

WHY A REQUEST NOW HAS A CHOICE. `/goals/{pid}/thread` rendered
`goals.go_thread_id` and nothing else, because that was the only GO
conversation a goal had. An approved autonomous errand opens a thread of
its own on the GO, and without a way to name one, every errand
conversation exists in the database and on no page.

SO `thread_id` IS OPTIONAL AND THE DEFAULT IS UNCHANGED. Omitted -- which
is every link in the product today -- resolves to the goal's main GO
thread exactly as before.

WHAT IS CHECKED, AND WHY EACH ONE. A thread id arrives in a query string,
so it is a claim, not a fact:

  IT MUST BELONG TO THIS GOAL. Otherwise `?thread_id=` on a goal the
  caller is a member of reads a conversation from one they are not --
  the cross-tenant shape that `_guard_thread_in_goal` exists to stop in
  chat.py, and it costs one predicate to not repeat it here.

  IT MUST BE A GO THREAD. This page renders a GO conversation: it opens
  a GO session and forwards what you type to the GO. Pointed at an
  assistant's thread it would relay the wrong agent's history into the
  GO's context and post the GO's replies into the assistant's
  conversation.

  IT MUST NOT BE ONE OF THE OTHER NAMED THREADS. A goal also has
  `orchestrator_thread_id`, `council_thread_id` and
  `security_thread_id`, all GO-owned and all rendered by their own
  routes with their own framing.

REFUSED, NOT SILENTLY DEFAULTED. Falling back to the main thread on a
bad id would show somebody a conversation they did not ask for and let
them type into it believing it was the errand's -- and a caller probing
ids would get a 200 every time. `None` means refuse; the callers turn
that into a 404 or a closed socket.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def resolve(pool, goal: dict, thread_id: str | None) -> str | None:
    """The GO thread to render, or None when the request must be refused.

    `goal` is the already-fetched row, so this adds at most one query and
    none at all for the default case.
    """
    main = goal.get("go_thread_id") or ""
    if not thread_id or thread_id == main:
        return main or None

    named = {
        goal.get("orchestrator_thread_id") or "",
        goal.get("council_thread_id") or "",
        goal.get("security_thread_id") or "",
    }
    if thread_id in named:
        log.info("go thread pick: %s is a named channel, not an errand "
                 "thread", thread_id)
        return None

    row = await pool.fetchrow(
        "SELECT agent_id, story_id FROM threads "
        " WHERE id = $1 AND goal_id = $2 AND left_goal_at IS NULL",
        thread_id, goal.get("id"))
    if row is None:
        log.info("go thread pick: %s is not a live thread of goal %s",
                 thread_id, goal.get("id"))
        return None
    if not (row["agent_id"] or "").lower().startswith("go-"):
        log.info("go thread pick: %s belongs to %s, not the GO",
                 thread_id, row["agent_id"])
        return None
    return thread_id
