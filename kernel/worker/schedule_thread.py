"""A scheduled errand gets a thread of its OWN, like an imported one.

THE BUG. `errand_schedules.driving_thread_id` is captured at approval
time as whichever thread the approver happened to be in -- and every
thread belongs to a STORY. Approving proposal 903 from the thread of
errand `74262e1f` pinned the schedule's cards to that errand's
conversation: a finished piece of work from earlier the same day. Run
11908's plan-approval card was posted, correctly, into a thread nobody
was looking at, and the person watching their assistant saw nothing.

The resolver in `kernel_park_cards` is right to prefer
`driving_thread_id` -- "an autonomous errand belongs in the thread of
the person who approved it". The mistake is one indirection along:
"the person's thread" is not a well-defined thing, because a thread is
story-scoped. What the schedule actually knows is WHO, and what it needs
is a thread for THIS story belonging to that person.

So this FINDS the thread belonging to the schedule's own story and
drives the run with that. It does not create one: `public.threads` is
the control panel's table and this role is refused INSERT on it by
privilege. `errand_grants.approve` opens it when it creates the story,
which is the right side of that boundary and the moment somebody is
watching.

FAILING SOFT IS DELIBERATE. If the owner cannot be derived, this returns
the captured thread unchanged: a card in an odd conversation is worse
than a card nowhere, and this must never be the reason a scheduled run
does not start.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def ensure_schedule_thread(conn, goal_id: str, story_id: str,
                                 driving_thread_id: str | None) -> str | None:
    """A thread for THIS story, owned by whoever approved the schedule.

    Returns the thread id to drive the run with, or `driving_thread_id`
    unchanged when nothing better can be worked out.
    """
    if not story_id:
        # No story, no thread: `sweep` already refuses to create a run
        # without one, for exactly this reason.
        return driving_thread_id
    if not driving_thread_id:
        return None

    owner = await _owner_of(conn, driving_thread_id)
    if owner is None:
        logger.warning(
            "schedule thread: %s names thread %s, which is gone; leaving the "
            "card where it was", story_id, driving_thread_id)
        return driving_thread_id

    created_by, _agent_id = owner

    # ALREADY BOUND TO THIS STORY? Then it is the right thread and the
    # capture was fine -- the common case once this fix is in, because
    # the first run creates it and later runs find it.
    existing = await conn.fetchval(
        """
        SELECT id FROM threads
         WHERE goal_id = $1 AND story_id = $2 AND created_by = $3
           AND left_goal_at IS NULL
         ORDER BY created_at DESC LIMIT 1
        """, goal_id, story_id, created_by)
    if existing:
        return existing

    # AND NO FURTHER. Opening one is the CONTROL PANEL's job:
    # `public.threads` is its table and this role is refused INSERT on it
    # by privilege -- which is correct, and which this module learned by
    # logging "permission denied for table threads" on a live run while
    # the person waited for a card that never came.
    #
    # `errand_grants.approve` now opens the thread when it creates the
    # story, which is both the right side of the boundary and the moment
    # somebody is actually watching. Reaching here means that did not
    # happen; the captured thread is the honest fallback.
    logger.info("schedule thread: %s has no thread of its own; keeping %s",
                story_id, driving_thread_id)
    return driving_thread_id


async def _owner_of(conn, thread_id: str):
    """`(created_by, agent_id)` for a thread, or None when it is gone."""
    try:
        row = await conn.fetchrow(
            "SELECT created_by, agent_id FROM threads WHERE id = $1",
            thread_id)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("schedule thread: cannot read %s (%s)",
                       thread_id, exc)
        return None
    if row is None or not row["created_by"]:
        return None
    return row["created_by"], row["agent_id"]
