"""What the GO may drive: autonomous errands, and nothing else.

WHY THIS EXISTS AS A PREDICATE RATHER THAN A ROUTE. Adding `steer`,
`ask_executor`, `story_progress` and `story_status` to `GO_ROUTES` is
cheap precisely because nothing underneath them checks authority --
every one takes `actor_id` as an opaque string and performs no check at
all. That is what makes the membership a coarse yes-or-no per TOOL, and
it cannot express "yes, but only for this run".

Stories and hand-started errands stay with the assistant, and the reason
is not policy but access: that is where the human conversation is. A
story's whole life is a person and their assistant working through it. An
autonomous errand is the opposite -- a person approved a plan once and
left, and the GO is the only agent still watching.

TWO CONDITIONS, BOTH REQUIRED.

  IT IS AN ERRAND. `stories.work_kind = 'errand'`, checked positively.
  NULL means story (`kernel/obs/fields.py:37`), so `!= 'story'` would
  admit every row written before the column existed.

  IT IS AUTONOMOUS. A live grant in `kernel.errand_schedules` covers it.
  This is `standing_approval`'s predicate
  (`kernel/harness/park_approval.py:56-79`) MINUS THE PLAN-HASH PIN, and
  the omission is deliberate: `approved_plan_hash` is NULL until the
  first plan is approved (`pin_plan_to_grant`), so including it would
  refuse the GO on the first run -- the run it most needs to watch. The
  pin gates RUNNING UNATTENDED; this gates WHO MAY DRIVE. Two different
  questions about the same grant.

SO `revoked_at` DOES DOUBLE DUTY, and that is a feature: revoking a
grant both stops the errand running unattended and hands it back to the
assistant.

READ FRESH ON EVERY CALL, for the reason `standing_approval`'s own
docstring gives -- "a person can revoke between legs and that has to
take effect".

FAIL CLOSED. A database error refuses rather than allows: the whole
point is a boundary, and a boundary that opens when a query hiccups is
not one.
"""

from __future__ import annotations

import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

_IS_ERRAND = """
    SELECT COALESCE(work_kind, 'story') AS work_kind
      FROM stories WHERE id = $1 AND goal_id = $2
"""

# The grant, without the plan pin. See the module docstring.
_HAS_GRANT = """
    SELECT 1
      FROM kernel.errand_schedules s
     WHERE s.goal_id = $1
       AND s.approved_by IS NOT NULL
       AND s.revoked_at IS NULL
       AND (s.expires_at IS NULL OR s.expires_at > now())
       AND (s.story_id IS NULL OR s.story_id = $2)
     LIMIT 1
"""


# ── THE SAME QUESTION, ASKED BY LOCAL EXECUTION ──────────────────────
#
# Local execution refuses an AUTONOMOUS errand and admits a hand-started
# one, and the reason is this module's opening paragraph read the other
# way round: "stories and hand-started errands stay with the assistant
# ... that is where the human conversation is". A person working a story
# in their own editor IS that conversation. An autonomous errand is the
# case with nobody in the room, and it belongs to the GO and a pod.
#
# EXPORTED RATHER THAN RESTATED. The local gates need the same two
# conditions, and a second copy would drift -- silently, and in the
# direction that admits an errand the GO is also driving. `work_kind <>
# 'errand'` is the tempting shorthand and it is wrong: it would refuse
# the hand-started errands local execution is FOR.


def autonomous_errand_predicate(alias: str = "s") -> str:
    """A correlated SQL fragment: is the row at `alias` an autonomous errand?

    For WHERE clauses over many stories, where one round trip per row is
    not affordable. `is_autonomous_errand` is the single-row form; both
    spell the same two conditions and a test holds them to it.

    The inner alias is `es` so it cannot collide with a caller whose
    outer table is already `s` -- which every current caller's is.
    """
    return f"""(
        COALESCE({alias}.work_kind, 'story') = 'errand'
        AND EXISTS (
            SELECT 1 FROM kernel.errand_schedules es
             WHERE es.goal_id = {alias}.goal_id
               AND es.approved_by IS NOT NULL
               AND es.revoked_at IS NULL
               AND (es.expires_at IS NULL OR es.expires_at > now())
               AND (es.story_id IS NULL OR es.story_id = {alias}.id)
        )
    )"""


async def is_autonomous_errand(conn, goal_id: str, story_id: str) -> bool:
    """Both conditions, for one story. RAISES on a database error.

    Deliberately not swallowing: every caller is a boundary, and each
    wants to refuse in its own words. A helper that returned False on a
    failed query would open the boundary exactly when the database is
    unwell -- which is this module's `FAIL CLOSED` paragraph, undone by
    a convenience.
    """
    kind = await conn.fetchval(_IS_ERRAND, story_id, goal_id)
    if kind != "errand":
        return False
    return bool(await conn.fetchval(_HAS_GRANT, goal_id, story_id))


async def go_may_drive(conn, goal_id: str, story_id: str) -> Refused | None:
    """None when the GO may act on this story, a `Refused` otherwise."""
    if not (story_id or "").strip():
        return Refused("E_NO_STORY",
                       "say which errand: the GO drives one at a time and "
                       "a call with no story has no run to act on")

    try:
        kind = await conn.fetchval(_IS_ERRAND, story_id, goal_id)
    except Exception:                                       # noqa: BLE001
        logger.exception("go_may_drive: could not read %s in %s",
                         story_id, goal_id)
        return Refused("E_NOT_AN_ERRAND",
                       "could not establish that this is an autonomous "
                       "errand, so it is refused")
    if kind is None:
        return Refused("E_NO_STORY",
                       f"{story_id} is not a story of {goal_id}")
    if kind != "errand":
        return Refused(
            "E_NOT_AN_ERRAND",
            f"{story_id} is a story, and stories belong to the assistant "
            f"-- that is where the person working on it is. The GO drives "
            f"autonomous errands only.")

    try:
        granted = await conn.fetchval(_HAS_GRANT, goal_id, story_id)
    except Exception:                                       # noqa: BLE001
        logger.exception("go_may_drive: could not read the grant for %s",
                         story_id)
        return Refused("E_NOT_AUTONOMOUS",
                       "could not establish that a live grant covers this "
                       "errand, so it is refused")
    if not granted:
        return Refused(
            "E_NOT_AUTONOMOUS",
            f"{story_id} is an errand somebody started by hand, or its "
            f"grant has been revoked or has expired. It belongs to the "
            f"assistant that started it. The GO drives errands a person "
            f"has approved to run unattended.")
    return None
