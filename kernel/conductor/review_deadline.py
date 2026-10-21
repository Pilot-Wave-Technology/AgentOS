"""The two failures a reviewer cannot report on itself.

`done -> reviewed` is gated on a review existing, so a story with no
review never leaves `done` — and a story stuck at `done` keeps its
assistant thread bound, keeps its SHCA workspace protected, and, since
`reclaim` now stands off during the review phase, keeps its node. The
reviewer's own exhaustion path covers the case where it ran and gave up.
These two cover the cases where it could not.

L3 — THE PHASE THAT WILL NOT END. A wedged leg is neither progressing
nor terminal: no ceiling fires, because no generation is journalled.
Before `reclaim` learned to stand off this cost nothing much; now it
holds a node indefinitely, which is why this ships in the same change as
that guard rather than after it.

L4 — THE POD THAT VANISHED. Preemption, an OOM kill, a node drained, a
cluster outage: the execution never writes anything again, so nothing
in-pod can record the review. Only something outside can, which is the
whole reason this is not simply a longer timeout in the loop.

BOTH WRITE `not_produced` AND MEAN IT. The verdict is its own value and
renders as its own thing; only `approve` reads as clean. Marking
unreviewed work as reviewed-and-fine would be worse than the stranded
story this replaces — it would convert "nobody looked" into "somebody
looked and it was fine", which is the one claim the whole design exists
to make true.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# A review is meant to take one leg of a few minutes, with three legs of
# headroom. Twenty minutes is generous enough that a slow but working
# reviewer is never cut off, and short enough that a wedged one does not
# hold a node for an hour.
DEFAULT_MINUTES = 20
_ENV = "AGENTOS_REVIEW_DEADLINE_MINUTES"


def deadline_minutes() -> int:
    """Read per call, so a wedge can be shortened without a redeploy."""
    if _ENV not in os.environ:
        return DEFAULT_MINUTES
    try:
        return max(1, int(os.environ[_ENV]))
    except ValueError:
        logger.warning("%s is not a number; using %d", _ENV, DEFAULT_MINUTES)
        return DEFAULT_MINUTES


async def _overdue_phases(conn, minutes: int) -> list:
    """Executions still in the review phase past the deadline."""
    return list(await conn.fetch(
        """
        SELECT id, story_id FROM kernel.executions
         WHERE phase = 'review'
           AND state = 'RUNNING'
           AND updated_at < now() - ($1 || ' minutes')::interval
         ORDER BY id LIMIT 20
        """, str(minutes)))


async def _stranded_stories(conn, minutes: int) -> list:
    """`done` stories with no review and nothing left that could write one.

    NOT KEYED ON `last_reviewed_state`. That column is set to 'done'
    BEFORE the transition is attempted and the transition's failure is
    swallowed, so it reports reviewedness that may never have happened.
    The review ROW is the proof, and `stories.state` is the outcome.

    ONLY STORIES THAT OWE A REVIEW, and on `finish.owes_a_review` --
    the SAME predicate the finish path uses -- rather than a second
    rule written here. A review is for a human's coding story;
    errands, merge stories and the engine's own diff/fix plumbing
    reach `reviewed` by a different road, and nothing about them
    changes because a sweep noticed them later.

    This function had no such filter, and the consequence was not a
    wrong repair but a permanent one. `sweep` appended the finding
    FIRST and then hit `if eid is None: continue`, so a story with no
    execution -- which every `merge-*` story is -- was counted,
    logged, and left exactly as it was. On dev that was twenty rows,
    pinned at twenty since July, reprinted on every tick of both
    conductors. A number that cannot go down is a number people stop
    reading, which costs more than the rows do.

    AN EXECUTION MUST EXIST, because `owes_a_review` is keyed on one
    and the repair writes its verdict onto one. THE GOAL MUST BE
    ACTIVE, because deleting a goal leaves its stories behind: the
    reconcile line read `0 goal(s), 20 review_missing`, which is the
    contradiction written out -- nothing live to reconcile, twenty
    findings anyway.

    Filtered here rather than skipped in the caller, so that what the
    sweep REPORTS is what it can actually act on.
    """
    rows = list(await conn.fetch(
        """
        SELECT s.id AS story_id,
               (SELECT e.id FROM kernel.executions e
                 WHERE e.story_id = s.id
                 ORDER BY e.id DESC LIMIT 1) AS execution_id
          FROM stories s
         WHERE s.state = 'done'
           AND s.updated_at < now() - ($1 || ' minutes')::interval
           AND NOT EXISTS (SELECT 1 FROM story_reviews r
                            WHERE r.story_id = s.id
                              AND r.reviewed_state = 'done')
           AND NOT EXISTS (SELECT 1 FROM kernel.executions e
                            WHERE e.story_id = s.id
                              AND e.phase = 'review'
                              AND e.state = 'RUNNING')
           AND EXISTS (SELECT 1 FROM kernel.executions e3
                        WHERE e3.story_id = s.id)
           AND EXISTS (SELECT 1 FROM goals g
                        WHERE g.id = s.goal_id AND g.status = 'active')
         ORDER BY s.updated_at LIMIT 20
        """, str(minutes)))

    # THE SHARED PREDICATE, applied per row. It reads the EXECUTION's
    # kind rather than the story's, which is why it cannot be folded
    # into the SQL above without restating its rules -- and restating
    # them is how the two definitions drift apart.
    from kernel.worker.finish import owes_a_review

    kept = []
    for row in rows:
        try:
            if await owes_a_review(conn, row["execution_id"]):
                kept.append(row)
        except Exception:                                   # noqa: BLE001
            # A predicate that cannot answer must not silently drop a
            # genuinely stranded story: keep it and let the repair
            # decide. The opposite default loses the rows this whole
            # function exists to find.
            logger.exception("owes_a_review: story %s", row["story_id"])
            kept.append(row)
    return kept


async def sweep(conn, *, dry_run: bool = False) -> list[dict]:
    """End overdue review phases, and review-by-default what nothing can.

    Returns one action per row touched. Never raises into the tick: a
    sweep that dies takes the pass with it, and the pass is also what
    reaps pods.
    """
    from kernel.worker.review_finish import synthesise_not_produced

    minutes = deadline_minutes()
    actions: list[dict] = []

    for row in await _overdue_phases(conn, minutes):
        actions.append({"execution_id": row["id"], "story": row["story_id"],
                        "action": "review_deadline",
                        "detail": f"review phase exceeded {minutes}m"})
        if dry_run:
            continue
        try:
            await synthesise_not_produced(
                conn, row["id"],
                f"the review phase ran past {minutes} minutes without a "
                f"verdict and was ended so the story could move")
        except Exception:                                   # noqa: BLE001
            logger.exception("review deadline: execution %s", row["id"])

    for row in await _stranded_stories(conn, minutes):
        eid = row["execution_id"]
        actions.append({"execution_id": eid, "story": row["story_id"],
                        "action": "review_missing",
                        "detail": "done with no review and nothing running"})
        # `eid is None` is now excluded by the query, not handled here;
        # keeping the guard means a future edit that drops the
        # predicate degrades to the old silent behaviour instead of
        # failing. Kept, but it should never fire.
        if dry_run or eid is None:
            continue
        try:
            await synthesise_not_produced(
                conn, eid,
                "the run that should have reviewed this story is gone -- "
                "the pod was lost, or the review never started")
        except Exception:                                   # noqa: BLE001
            logger.exception("review backstop: story %s", row["story_id"])

    if actions:
        logger.warning("review sweep: %d story/stories recorded "
                       "not_produced", len(actions))
    return actions
