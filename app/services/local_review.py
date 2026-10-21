"""The story review a local run owes, recorded the same way a pod's is.

WHAT THE POD DOES, AND WHICH PARTS CARRY OVER. `kernel/worker/finish.py`
keeps a finished run alive with `phase = 'review'` and a fresh generation,
and the next leg resolves the review profile IN THE SAME POD, on the same
workspace, with no memory of the work. Three properties come from that:

  the workspace is intact      the reviewer reads the tree the executor
                               wrote, with its git history
  the surface cannot write     every writing tool is refused BY NAME, so
                               "the reviewer cannot change the thing it is
                               judging because it holds no tool that could"
  the reviewer is cold         "You did not write it... You have no memory
                               of the work being done"

LOCALLY THE FIRST HOLDS, THE SECOND AND THIRD DO NOT, and that is a
decision rather than an oversight. The review runs in the author's own
session, so it has the whole build in context and holds every tool Claude
Code gives it. The prompt therefore has to ASK for what the pod's profile
GUARANTEES, and a prompt is weaker than an absent tool. Recorded here so
nobody later reads the local verdict as carrying the same weight.

WHAT IS IDENTICAL, deliberately: the verdicts, the minimum summary, the
`approve`-with-unexamined-churn downgrade, the rendered markdown, and the
writer. A review means one thing on the board whichever lane produced it,
and the downgrade is the rule that stops "nobody looked" being recorded as
"somebody looked and it was fine".
"""

from __future__ import annotations

import logging

from kernel.harness.tools_worker.review import MIN_SUMMARY, VERDICTS, _render

logger = logging.getLogger(__name__)

# The kinds that reach `reviewed` without one, mirroring `owes_a_review`.
# An errand's deliverable is an artifact already measured mechanically; a
# merge story is somebody else's reviewed work rolled up; the engine's
# diff/fix stories are its own plumbing.
_EXEMPT_WORK_KINDS = ("errand",)


class ReviewRefused(ValueError):
    """Named, so the refusal says which rule and what to do about it."""


async def owes_a_review(pool, story_id: str) -> bool:
    """Whether this local story should be reviewed before reaching `reviewed`.

    FAIL CLOSED ON A MISSING PROFILE, like the pod's: without that, a
    deployment whose local profile tree lacks the review directory would
    hold the story open waiting for a review nothing can produce. Better to
    finish unreviewed, loudly, than to park in a state nothing serves.
    """
    row = await pool.fetchrow(
        "SELECT COALESCE(work_kind, 'story') AS work_kind, story_kind, created_by "
        "  FROM stories WHERE id = $1", story_id)
    if row is None:
        return False
    if row["work_kind"] in _EXEMPT_WORK_KINDS:
        return False
    if (row["story_kind"] or "") == "merge" or (row["created_by"] or "") == "engine":
        return False

    from kernel.profiles.local_registry import resolve_local
    from kernel.profiles.record import UnknownProfileError
    from kernel.profiles.story_keys import derive_kind_domain

    kind, domain = derive_kind_domain(row["work_kind"])
    try:
        resolve_local(kind, domain, "review")
    except UnknownProfileError:
        logger.warning(
            "story %s would owe a review but no LOCAL %s/%s/review profile "
            "exists — finishing unreviewed", story_id, kind, domain)
        return False
    return True


async def record_local_review(pool, story_id: str, goal_id: str,
                              args: dict) -> dict:
    """Validate a locally-authored review, record it, move the story.

    Raises `ReviewRefused` with a sentence the session can act on -- the
    reader is the agent that just called this, and one correction costs a
    message where a bare rejection costs the whole cycle.
    """
    verdict = str(args.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ReviewRefused(
            f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}")

    summary = str(args.get("summary") or "").strip()
    if len(summary) < MIN_SUMMARY:
        raise ReviewRefused(
            f"`summary` is {len(summary)} characters and a review needs at "
            f"least {MIN_SUMMARY}. Say what you read, what you concluded, and "
            f"why. If you could not read the changes, say that and use "
            f"verdict `code_not_reviewed`.")

    # THE DOWNGRADE, identical to the pod's and for the same reason: a
    # reviewer that skimmed and approved converts "nobody looked" into
    # "somebody looked and it was fine", which is the one claim this design
    # exists to make true. Recorded in the review rather than refused.
    downgraded = ""
    not_examined = [str(x) for x in (args.get("not_examined") or []) if x]
    if verdict == "approve" and not_examined:
        verdict = "partial"
        downgraded = (
            f"The reviewer answered `approve` while reporting "
            f"{len(not_examined)} unexamined item(s). `approve` means "
            f"everything with material churn was read.")

    text = _render(args, verdict, downgraded)

    from kernel.worker.review_finish import record_review_for_story

    # A CONNECTION, NOT THE POOL: the writer opens a transaction, and
    # `asyncpg.Pool` has none. Passing the pool is what left every local
    # story at `done` with no review row.
    async with pool.acquire() as conn:
        moved = await record_review_for_story(
            conn, story_id, goal_id,
            verdict=verdict, review_text=text,
            reviewer="local-session", run_id="local")

    logger.info("local review recorded story=%s verdict=%s moved=%s",
                story_id, verdict, moved)

    # DELIVERED HERE, NOT ONLY BY THE NOTIFY. The writer above announces
    # the review on `story_review_ready` for a listener to deliver -- the
    # only way for a pod's review, written by the conductor in another
    # process. A local review is written by THIS request, so it can be
    # delivered by this request, and then it does not depend on that
    # listener at all. On a91cac41 (feature, 2026-10-16) the listener's
    # connection was gone, the NOTIFY reached nobody, and the review never
    # got to the goal. `deliver` claims the row atomically, so when the
    # listener is alive and gets there too, the review still goes out once.
    if moved:
        from app.services.story_review_delivery import deliver
        try:
            await deliver(goal_id, story_id, text)
        except Exception:                                   # noqa: BLE001
            logger.exception("local review: delivery failed for %s — the "
                             "review is recorded", story_id)
    return {"ok": True, "verdict": verdict, "downgraded": bool(downgraded),
            "reviewed": bool(moved), "review_text": text}
