"""When the next errand check-in is due, and how far apart they are.

WHY THIS IS NOT A `% 900` ON THE CLOCK, which is what it was. The
cadence was `int(time.time()) // WINDOW_S`, a wall-clock bucket: at most
one wake per fifteen-minute slice of the day. That bounds the cost
correctly and says nothing at all about the gap between two wakes. On
run 306 the gap came out at 3m40s because the run happened to start near
a boundary; the next one could be fourteen minutes. An operator watching
that cannot tell a working feature from a broken one, and the first
thing anybody did with it was try.

SO THE GATE IS ELAPSED TIME SINCE THE LAST WAKE FOR THIS RUN, which is
the thing the interval was always meant to mean. `go_wake_log` already
records every wake with its time, so there is no new state to keep.

AND THE FIRST CHECK-IN IS IMMEDIATE. A run with no prior wake is due
now, not at the next boundary -- which is also the answer to "does it
fire when the errand starts", a question the bucket made genuinely hard
to answer by watching.

THE REF STILL DEDUPES, because the gate alone cannot: two replicas
sweeping at the same moment both read the same last-wake time and both
decide yes. A minute-granular ref makes the unique index settle it, the
way it already settled the bucket.
"""

from __future__ import annotations

import time

from app.services import platform_settings

DEFAULT_S = 900

# A FLOOR OF A MINUTE, because the sweep itself runs once a minute and a
# smaller number would promise a cadence the loop cannot deliver -- the
# effective gap is always the window rounded up to the next sweep.
MIN_S = 60

# A CEILING OF A DAY. Not a safety limit -- a typo limit. `9000000`
# entered for `900` would switch the feature off silently, which is the
# one failure mode a settings box invites.
MAX_S = 86_400


async def window_s() -> int:
    """Seconds between check-ins on one running errand."""
    raw = await platform_settings.get_int(
        platform_settings.CHECKIN_SECONDS, DEFAULT_S)
    return max(MIN_S, min(MAX_S, raw))


async def quiet_s(pool, goal_id: str, execution_id: int) -> int | None:
    """Seconds since this run's last check-in, or None if never.

    SCOPED TO THE RUN by the ref prefix, not to the goal: a goal with two
    errands going would otherwise have each one's wake reset the other's
    clock, and the busier errand would silence the quieter one.
    """
    return await pool.fetchval(
        "SELECT EXTRACT(EPOCH FROM (now() - max(created_at)))::int "
        "  FROM go_wake_log "
        " WHERE goal_id = $1 AND kind = 'errand_progress' "
        "   AND ref LIKE $2", goal_id, f"{execution_id}:%")


def ref(execution_id: int) -> str:
    """The dedupe key for one check-in."""
    return f"{execution_id}:{int(time.time()) // 60}"


# THE STATES A RUN SITS IN BEFORE IT HAS STARTED. Nothing has begun in
# any of them, so nothing can have stalled: a `stall_reason` here is the
# QUEUE talking -- no node with room yet -- not the work.
PRE_START = ("PENDING", "ADMITTED", "DISPATCHING")


def is_due(quiet: int | None, age_s: int | None, window: int) -> bool:
    """Has this run gone `window` seconds without a check-in?

    RUN 345 DIED OF THE `None`. `quiet_s` returns None when a run has
    never been checked in, and the gate read
    `if quiet is not None and quiet < window: continue` -- so None fell
    straight through and the FIRST check-in on every run fired on the
    next sweep. With a 600s window configured, run 345's first check-in
    arrived 28 seconds after dispatch.

    That was the whole incident. The pod was Unschedulable, which
    `promote_row` documents as normal for up to ninety seconds; the
    digest reported the stall; GO cancelled at eighty. Honouring the
    window would have put GO's first look at ten minutes, by which time
    the pod was placed and there was nothing to report.

    So a run that has never been checked in is due on its AGE, measured
    from when the run was created -- the same clock a person reading
    "check in every 600 seconds" would expect.
    """
    return (quiet if quiet is not None else (age_s or 0)) >= window
