"""A story waiting for a merge nobody is going to perform.

WHAT THIS EXISTS FOR. A coding story now holds its pod between push and
merge, because that is the whole point: the person can ask for one more
thing, and the tree is still there. The cost is that a PR nobody merges
holds a pod and its workspace claim indefinitely, and nothing else would
ever notice -- the execution is parked, which is a healthy state, and
the story is `awaiting_merge`, which is a correct state. There is no
error anywhere. It just never ends.

IT REAPS THE COMPUTE, NOT THE STORY, and that distinction is the whole
design. An earlier version of this moved the story to `blocked` and
stopped there, on the theory that `reclaim` would collect on the signal
it already trusts. It would not have: `reclaim` releases on the story
reaching `done`, `reviewed`, `cancelled` or `rejected`, or on the
EXECUTION reaching `CANCELLED` or a stale `FAILED`. `blocked` is in
neither list, so that janitor would have run, logged, moved the row --
and freed nothing at all. The leak it was written for would have
survived it, now with a tidy audit trail saying otherwise.

SO IT STANDS THE RUN DOWN: the landing park is retired and the
execution is CANCELLED, which is a signal `reclaim` already acts on. No
pod is touched from here. A janitor that deleted compute directly would
be a second authority over the same resource, free to disagree with the
first at exactly the wrong moment.

AND THE STORY STAYS `awaiting_merge`, which is the honest state: the
branch really is pushed, the PR really is open, and nobody has merged
it. Moving it to `blocked` would say somebody must unblock it; moving
it to `done` would claim the work landed. Neither is true, and a story
sitting at `awaiting_merge` with no pod costs nothing -- the expensive
thing has already been released. If the person merges the PR a week
later, `story_landing.sweep_landed` takes the cold path and the story
reaches `done` then, which is exactly right.
"""
from __future__ import annotations

import asyncio
import logging

from app.database import get_pool

_log = logging.getLogger("app.awaiting_merge_janitor")

SWEEP_INTERVAL_SECONDS = 900          # 15 min — a merge is a human act, not a tick
STALE_AFTER_HOURS = 72                # three days of nobody merging it

# A WARNING BEFORE THE DEADLINE, because the first a person should hear of this
# is not the pod being taken away. One notice, at this age, then the deadline
# does the rest.
WARN_AFTER_HOURS = 24


async def run_loop() -> None:
    _log.info("awaiting_merge janitor started (%ds interval, stale after %dh)",
              SWEEP_INTERVAL_SECONDS, STALE_AFTER_HOURS)
    await asyncio.sleep(120)          # let boot settle
    while True:
        try:
            await sweep_once()
        except Exception:             # noqa: BLE001
            _log.exception("awaiting_merge sweep failed — non-fatal, retry next cycle")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def waiting(pool, *, hours: int = STALE_AFTER_HOURS) -> list[dict]:
    """Runs parked on a merge for longer than `hours`.

    BOTH PARKS COUNT. A run waiting for a merge (`landing`) and one
    waiting for a person to close the story after it merged
    (`closeout`) hold exactly the same pod, and a deadline on only
    one of them is a deadline on neither -- the second park is
    reached by every story that lands, so it would become the usual
    place to leak.

    KEYED ON THE PARK, not on the story's `updated_at`. The story row is
    touched by anything that edits it -- a title change, a resource
    link -- and a person tidying the board would silently reset a
    deadline measured in days. The question's `created_at` is the moment
    the wait actually began and nothing else writes it.

    Ordered oldest first so a log line reads as a queue and the worst
    case is the one a reader sees.
    """
    rows = await pool.fetch(
        """
        SELECT e.id AS execution_id, q.id AS question_id,
               s.id AS story_id, s.goal_id, s.title,
               EXTRACT(EPOCH FROM (now() - q.created_at)) / 3600.0 AS hours_waiting
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
          JOIN stories s ON s.id = e.story_id
         WHERE q.kind IN ('landing', 'closeout')
           AND q.status = 'OPEN'
           AND s.state = 'awaiting_merge'
           AND q.created_at < now() - ($1 || ' hours')::interval
         ORDER BY q.created_at ASC
        """,
        str(int(hours)),
    )
    return [dict(r) for r in rows]


async def sweep_once() -> int:
    """Stand down runs past the deadline. Returns how many were released."""
    pool = get_pool()
    released = 0
    for row in await waiting(pool):
        try:
            if await _stand_down(pool, row):
                released += 1
        except Exception:             # noqa: BLE001
            _log.exception("awaiting_merge: could not stand down run %s",
                           row["execution_id"])
    released += await _release_local(pool)
    return released


# A LOCAL story has no pod and no park, so nothing above can reach it --
# `waiting` is keyed on a `landing`/`closeout` question and a local story
# never has one. What it holds instead is the CHECKOUT LOCK, and that is
# exempted from the heartbeat reaper precisely because a merge wait
# outlasts a heartbeat. This is the other half of that bargain: the lock
# is not freed when the editor closes, and it IS freed when the merge
# never comes.
LOCAL_STALE_SQL = """
    SELECT id, title,
           EXTRACT(EPOCH FROM (now() - updated_at)) / 3600.0 AS hours_waiting
      FROM stories
     WHERE state = 'awaiting_merge'
       AND checked_out_to_uid IS NOT NULL
       AND updated_at < now() - ($1 || ' hours')::interval
     ORDER BY updated_at ASC
"""


async def _release_local(pool) -> int:
    """Free a local checkout nobody came back to. Returns how many.

    THE STORY STAYS `awaiting_merge`, exactly as the pod lane's does and
    for the same reason: the branch really is pushed and the PR really
    is open. What is released is the claim on it, so somebody else can
    pick the story up — which is the local equivalent of giving the pod
    back, and the only thing that was actually being held.
    """
    freed = 0
    for row in await pool.fetch(LOCAL_STALE_SQL, str(int(STALE_AFTER_HOURS))):
        try:
            moved = await pool.execute(
                "UPDATE stories SET checked_out_to_uid = NULL, "
                "    checkout_session_id = NULL, checked_out_at = NULL, "
                "    checkout_expires_at = NULL, updated_at = now() "
                " WHERE id = $1 AND state = 'awaiting_merge' "
                "   AND checked_out_to_uid IS NOT NULL", row["id"])
            if moved.split()[-1] != "0":
                freed += 1
                _log.warning(
                    "awaiting_merge: released the local checkout on story "
                    "%s after %.0fh — %s", row["id"], row["hours_waiting"],
                    (row["title"] or "")[:60])
        except Exception:             # noqa: BLE001
            _log.exception("awaiting_merge: could not release the local "
                           "checkout on story %s", row["id"])
    return freed


async def _stand_down(pool, row: dict) -> bool:
    """Retire the park and cancel the run, in one transaction.

    TORN, EITHER HALF IS WORSE THAN NEITHER. A retired park on a live
    run is a wait nothing will end and a run nothing will finish; a
    cancelled run with an open park trips `questions_one_open` against
    anything that asks later. Together they are one fact: this run is
    no longer waiting for anything.
    """
    reason = (f"No merge after {row['hours_waiting']:.0f}h. The branch is "
              f"pushed and its PR is open; the workspace was released so it "
              f"is not held indefinitely. Merging the PR still completes "
              f"this story.")

    async with pool.acquire() as conn, conn.transaction():
        # `status = 'OPEN'` is the CAS: a merge that landed between the
        # SELECT and here must win, and it wins by having already
        # answered this question.
        closed = await conn.execute(
            "UPDATE kernel.questions "
            "   SET status = 'ANSWERED', answer_text = $2, verdict = 'decline', "
            "       answered_by = 'awaiting-merge-janitor', answered_at = now() "
            " WHERE id = $1 AND status = 'OPEN'",
            row["question_id"], reason)
        if closed.split()[-1] == "0":
            return False

        # CANCELLED is the signal `reclaim` already acts on, which is the
        # only reason this janitor frees anything at all. See the module
        # docstring for the version of this that did not.
        await conn.execute(
            "UPDATE kernel.executions "
            "   SET state = 'CANCELLED', updated_at = now() "
            " WHERE id = $1 AND state IN ('PARKED_HOT', 'PARKED_COLD')",
            row["execution_id"])

    _log.warning("awaiting_merge: run %s stood down after %.0fh — %s",
                 row["execution_id"], row["hours_waiting"],
                 (row["title"] or "")[:60])
    return True
