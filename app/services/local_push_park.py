"""Twice refused for the same reason: stop retrying, ask a person.

THE EXTENSION RE-SUBMITS ON A TIMER while a story sits at `submitting`.
That is right for a flake -- a dropped connection, a token that expired
mid-push -- and wrong for everything else. A protected branch, a revoked
installation, a repository that moved: all refuse identically for ever,
and the retry produces one identical card a minute with nobody told it
will never succeed. Exec 407 was this exact mechanism in the pod lane.

THE SECOND IDENTICAL REFUSAL PARKS IT. Not the first: a flake retried
once and succeeded should cost nothing and say nothing. Identical means
the reason AND the detail -- the same cause twice is a wall, two
different causes is two problems and the second deserves its attempt.

PARKED MEANS `blocked`, WHICH IS A STATE THAT ALREADY MEANS THIS. The
board shows it, the recover actions (replan / retry / abandon) already
apply to it, and `storyWatch` stops retrying because the phase is no
longer `submitting`. Nothing new had to be taught about it.
"""

from __future__ import annotations

import hashlib
import logging

_log = logging.getLogger(__name__)

# Two. Named rather than inlined, because the whole design is an
# argument about this number and a reader should find it stated.
PARK_AFTER = 2


def _hash(detail: str) -> str:
    return hashlib.sha256((detail or "").encode("utf-8")).hexdigest()[:32]


async def clear(pool, story_id: str) -> None:
    """A successful submit. The streak is "in a row", so success ends it."""
    await pool.execute(
        "DELETE FROM local_push_refusals WHERE story_id = $1", story_id)


async def note(pool, story_id: str, kind: str, error: str) -> int:
    """Record one refusal. Returns the streak length for this cause."""
    return int(await pool.fetchval(
        """
        INSERT INTO local_push_refusals
               (story_id, kind, detail_hash, last_error)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (story_id, kind) DO UPDATE
           SET streak = CASE
                   WHEN local_push_refusals.detail_hash = EXCLUDED.detail_hash
                   THEN local_push_refusals.streak + 1
                   ELSE 1 END,
               detail_hash = EXCLUDED.detail_hash,
               last_error = EXCLUDED.last_error,
               last_at = now()
        RETURNING streak
        """, story_id, kind, _hash(error), (error or "")[:2000]) or 1)


async def park_if_stuck(pool, story_id: str, refusal: dict) -> dict:
    """Count this refusal, and park the story if it is the second alike.

    THE REFUSAL IS RETURNED EITHER WAY, enriched rather than replaced.
    The caller is answering a submit and still has to say no; what
    changes is that the answer now also says "and I have stopped trying",
    which is the part a person needs in order to act.
    """
    kind = str(refusal.get("reason") or "push_failed")
    error = str(refusal.get("error") or "")
    try:
        streak = await note(pool, story_id, kind, error)
    except Exception:                                       # noqa: BLE001
        _log.exception("local push park: could not count the refusal for %s",
                       story_id)
        return refusal
    if streak < PARK_AFTER:
        return refusal

    try:
        await _park(pool, story_id, kind, error, streak)
    except Exception:                                       # noqa: BLE001
        _log.exception("local push park: could not park story %s", story_id)
        return refusal
    return {**refusal, "parked": True,
            "error": error + (
                f"\n\nThat is {streak} identical failures, so I have stopped "
                "retrying and parked the story. It needs a person: check the "
                "branch protection, the app installation and the repository, "
                "then use Retry.")}


async def _park(pool, story_id: str, kind: str, error: str, streak: int) -> None:
    from app.services.local_report import report_message  # noqa: PLC0415

    await pool.execute(
        "UPDATE stories SET state = 'blocked', current_phase = 'blocked', "
        "       updated_at = now() WHERE id = $1 "
        "   AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')",
        story_id)
    _log.warning("local story %s parked after %d identical %s refusals",
                 story_id, streak, kind)
    await report_message(
        story_id, "agent",
        f"The push has been refused {streak} times for the same reason, so I "
        "have stopped retrying.\n\n> " + (error or kind),
        card_data={"kind": "push_parked", "reason": kind, "streak": streak})
