"""Is this thread an autonomous errand's own conversation?

WHY THE QUESTION EXISTS. System wakes -- `[ERRAND PARKED]`, `[ERRAND
PROGRESS]` -- and the kernel's answers are how an autonomous errand
actually proceeds. GO is woken by them, acts on them and reports back,
and a person watching the errand needs to see that happening or the
autonomy is a black box: work appears, decisions are made, and the
thread shows only GO's occasional summary.

Everywhere else those same rows are noise. A goal's main thread, an
assistant's chat and a council thread all receive system rows that are
plumbing -- memory flushes, context injections, routing wakes -- and
rendering them would bury the conversation a person came to read. That
is why the fan-out filters them out today, and that filter is right for
every surface except this one.

So the rule is narrow on purpose: an errand thread, and only while the
errand is genuinely autonomous. Revoke the grant and the thread goes
quiet again on the next message, with no deploy and nothing to remember.

THE GRANT NAMES THE THREAD ITSELF. `errand_schedules.driving_thread_id`
is set when the errand is approved, so this is one indexed lookup and
needs no join back through stories or executions -- and it cannot drift
from the grant, because it IS the grant.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# Same clauses as `kernel/go/drivable._HAS_GRANT`. A second definition of
# "autonomous" disagrees the first time one moves, and here the
# disagreement is a person either drowning in plumbing or watching a
# blank thread.
_SQL = """
    SELECT 1
      FROM kernel.errand_schedules s
     WHERE s.driving_thread_id = $1
       AND s.approved_by IS NOT NULL
       AND s.revoked_at IS NULL
       AND (s.expires_at IS NULL OR s.expires_at > now())
     LIMIT 1
"""


async def is_autonomous_errand_thread(db, thread_id: str) -> bool:
    """True when system and kernel rows belong on this thread's page.

    FAILS CLOSED. An error here means the caller cannot tell, and the
    safe answer is the behaviour every other surface already has --
    filter the plumbing out. Showing it by mistake on an assistant's
    chat would be a visible regression on a page that was fine.
    """
    if not thread_id:
        return False
    try:
        return bool(await db.fetchval(_SQL, str(thread_id)))
    except Exception:                                       # noqa: BLE001
        _log.warning("errand visibility: could not read the grant for "
                     "thread %s; hiding system rows", thread_id,
                     exc_info=True)
        return False
