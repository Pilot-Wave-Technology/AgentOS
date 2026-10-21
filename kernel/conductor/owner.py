"""Whose concurrency budget does this execution spend?

WHY ADMISSION NEEDED A NEW KEY. Buckets were keyed
`(goal_id, domain, kind)` with a ceiling of four. That answers "how much
coding work may this goal run", which is not a question anybody asked --
and it meant a goal doing coding AND business work got two independent
budgets, while a goal doing one kind was capped at four however many
people were waiting.

The limit people actually want is per WHO:

    the GO's autonomous errands     10 per goal
    each person's assistant          5 per goal
                                    20 across the whole system

PER GOAL, NOT PER PERSON. Somebody with an assistant in two goals gets
five in each -- the goal is part of the key, so the two budgets are
separate and neither starves the other. Ten of their own, if the system
has room.

THE ERRAND TEST IS THE ONE ALREADY IN USE. `kernel/go/drivable.py`
decides whether GO may drive a run: `work_kind = 'errand'` plus a live
grant. Reused verbatim rather than restated, because two definitions of
"autonomous errand" would disagree the first time one was edited -- and
the disagreement would be a run counted against the wrong budget, which
looks like a limit that is simply wrong.

COMPUTED ONCE, THEN STORED. `executions.admission_owner` is written the
first time admission looks at a row and read from then on. Ownership
must not change mid-life: a run that starts on the GO's budget and later
answers as an assistant's would release a slot into a bucket it never
took one from, and the two counts would drift apart with nothing saying
so.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

GO = "go"
ASSISTANT_PREFIX = "assistant:"

# What each kind of owner may run at once, within one goal.
CEILINGS = {GO: 10}
ASSISTANT_CEILING = 5

# An execution whose owner cannot be worked out. It gets the assistant
# ceiling under a goal-wide name rather than its own bucket: unknown
# ownership must not mean unlimited, and must not mean zero either.
UNKNOWN = "assistant:unattributed"

_STORY = """
    SELECT s.id, COALESCE(s.work_kind, 'story') AS work_kind,
           COALESCE(s.assigned_to, s.created_by, '') AS who
      FROM stories s WHERE s.id = $1
"""

# Verbatim from kernel/go/drivable.py's grant check. A live, unrevoked,
# unexpired grant for this goal, either goal-wide or pinned to this
# story.
_GRANT = """
    SELECT 1
      FROM kernel.errand_schedules s
     WHERE s.goal_id = $1
       AND s.approved_by IS NOT NULL
       AND s.revoked_at IS NULL
       AND (s.expires_at IS NULL OR s.expires_at > now())
       AND (s.story_id IS NULL OR s.story_id = $2)
     LIMIT 1
"""


def ceiling_for(owner: str) -> int:
    """How many this owner may run at once in one goal."""
    return CEILINGS.get(owner, ASSISTANT_CEILING)


async def resolve(conn, execution_id: int, goal_id: str,
                  story_id: str | None) -> str:
    """Who owns this run. Cheap after the first call.

    NEVER RAISES. Admission runs every tick over every pending row; an
    owner lookup that could throw would stop the whole pass, and a run
    nobody can attribute should queue somewhere rather than block
    everybody. On any failure it returns `UNKNOWN`, which has the
    assistant ceiling.
    """
    try:
        stored = await conn.fetchval(
            "SELECT admission_owner FROM kernel.executions WHERE id = $1",
            execution_id)
        if stored:
            return stored

        owner = UNKNOWN
        if story_id:
            row = await conn.fetchrow(_STORY, story_id)
            if row is not None:
                if row["work_kind"] == "errand" and await conn.fetchval(
                        _GRANT, goal_id, story_id):
                    # An APPROVED autonomous errand. A hand-started
                    # errand has no grant and belongs to whoever started
                    # it, which is the assistant's budget.
                    owner = GO
                elif row["who"]:
                    owner = f"{ASSISTANT_PREFIX}{row['who']}"

        await conn.execute(
            "UPDATE kernel.executions SET admission_owner = $2 "
            " WHERE id = $1 AND admission_owner IS NULL",
            execution_id, owner)
        return owner
    except Exception as exc:                                # noqa: BLE001
        logger.warning("admission owner: could not attribute execution %s "
                       "(%s); using %s", execution_id, exc, UNKNOWN)
        return UNKNOWN
