"""Tell the person when their unattended errand needs them.

THE HOLE THIS CLOSES. An autonomous errand that parks on a kind only a
human may settle -- `acceptance`, `direction`, `escalation`,
`permission` -- goes quiet. Correctly: the scheduled check-ins are
suppressed because there is nothing GO can do, and waking it to watch
somebody else's decision is the waste the whole wake design exists to
avoid.

But then nothing happens at all. The park event wakes GO once, GO says
"this needs a person" on a thread, and that is the end of it -- on an
errand whose entire premise is that nobody is watching the thread. The
run waits indefinitely behind one message.

GO'S OWN QUESTION CAN CAUSE THIS. `ask_executor` parks the run on a
`direction` when the answer turns out to need a decision, and GO now
asks on every check-in. So the agent can strand its own errand and then
correctly fall silent about it.

SO THE NOTICE GOES TO A PERSON, NOT TO GO. The decision is theirs, the
card is already in their thread, and another GO turn would only repeat
what GO already said. This is the bell badge they already have.

THE APPROVER, specifically. They approved the errand knowing it would
run unattended, the ledger records them as the principal and the cost is
theirs -- so they are who owes it an answer, and who is surprised if it
silently never finishes.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# One notice per question, through the table every other wake dedupes
# on. NOT `_deliver`, which would also enqueue a GO turn -- the point
# here is precisely that GO has nothing to do.
_CLAIM = """
    INSERT INTO go_wake_log (goal_id, kind, ref)
    VALUES ($1, 'errand_person_notice', $2)
    ON CONFLICT (goal_id, kind, ref) DO NOTHING
    RETURNING id
"""

_WHO = """
    SELECT s.approved_by
      FROM kernel.errand_schedules s
     WHERE s.goal_id = $1
       AND s.approved_by IS NOT NULL
       AND s.revoked_at IS NULL
       AND (s.expires_at IS NULL OR s.expires_at > now())
       AND (s.story_id IS NULL OR s.story_id = $2)
     ORDER BY (s.story_id IS NOT NULL) DESC
     LIMIT 1
"""


async def notify(pool, *, goal_id: str, story_id: str, thread_id: str,
                 question: int, kind: str, title: str) -> bool:
    """Tell the approver their errand is waiting. True if a notice went.

    NEVER RAISES. This runs off the park path; a notification that fails
    must not take the park wake with it, because the wake is the thing
    that at least gets it onto a thread.
    """
    try:
        uid = await pool.fetchval(_WHO, goal_id, story_id or "")
        if not uid:
            return False
        if await pool.fetchval(_CLAIM, goal_id, f"q:{question}") is None:
            return False                       # already told them
        from app.notifications import push_notification

        await push_notification(
            uid,
            type="errand_needs_you",
            title=f"Your errand needs a decision: {title or story_id}",
            # WHAT IT IS AND WHAT STOPS UNTIL THEY ANSWER. A notice that
            # says only "needs attention" makes them open the page to
            # find out whether it is urgent, which is the cost it was
            # supposed to save.
            body=(f"It parked on a {kind} and cannot go further until "
                  f"you answer. Nothing else is waiting on it, and the "
                  f"orchestrator has stopped checking in because there "
                  f"is nothing it can do here."),
            goal_id=goal_id, thread_id=thread_id,
            link=f"/goals/{goal_id}/threads/{thread_id}",
        )
        return True
    except Exception:                                       # noqa: BLE001
        _log.exception("errand notice failed for goal %s question %s",
                       goal_id, question)
        return False
