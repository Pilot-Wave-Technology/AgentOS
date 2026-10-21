"""A local story's cycles: which one is open, and what ends it.

A CYCLE IS ONE AGREED INTENT, from a plan being approved to the work
landing. It is not a plan version -- a parked plan may be amended any
number of times while a person reads it, and all those versions are the
same cycle, the same `plan-NN.md`, the same thing being agreed.

THE READERS WERE SCATTERED AND THE WRITER WAS ANONYMOUS. The cycle number
was derived in `_default_plan_path` as `1 + MAX(version)`, the "is a cycle
open" question was asked by three modules each as its own SQL, and the
SPEND -- the single line that closes a cycle -- was an UPDATE inline in
whichever function happened to be closing it. That is three ways to get
the same answer and no way to find out who ended a cycle. All of it lives
here now, and the spend takes the reason as an argument so the log says
which event closed it.

WHERE A CYCLE IS SPENT, and these are the only places:
  * the SUBMIT (`local_diff._park_for_review`) -- the work is pushed and
    the pull request is open, so the intent is delivered;
  * marking the story done (`local_completion.finish_local_story`) -- the
    story is finished with, so its plan is too.
A landed diff mid-plan deliberately does NOT spend one: a person lands a
safe subset of an agreed plan all the time, and spending it there would
refuse the rest of the work they already approved.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PLAN_FOLDER = "agentos_plans"

# Catch-up plans (Phase 5) take the trunk in and re-run checks. They are
# not a new intent, so they must not consume a cycle number -- doing so
# would renumber the plan files underneath a person mid-story.
WORK = "work"


async def current_cycle(pool, story_id: str) -> int:
    """The cycle this story is on. 1 before anything has been approved.

    COUNTED FROM `cycle`, FALLING BACK TO `version`. The column is new and
    nullable: every row written before the migration has it backfilled, but
    the six other writers of `plan_versions` -- none of which this plan
    edits -- still INSERT without it, so a NULL has to read as "the version
    is the cycle", which is what it was.
    """
    n = await pool.fetchval(
        "SELECT COALESCE(MAX(COALESCE(cycle, version)), 0) FROM plan_versions "
        " WHERE story_id = $1 AND COALESCE(kind, 'work') = 'work'", story_id)
    return max(1, int(n or 0))


async def next_cycle(pool, story_id: str) -> int:
    """The number the NEXT approved plan will carry.

    One more than the cycles already finished -- and a cycle still open is
    not finished, so an amendment reuses its number and overwrites one
    `plan-NN.md` rather than leaving ten near-identical files.
    """
    if await cycle_is_open(pool, story_id):
        return await current_cycle(pool, story_id)
    return await current_cycle(pool, story_id) + (
        1 if await has_any_plan(pool, story_id) else 0)


async def cycle_is_open(pool, story_id: str) -> bool:
    """Is there an approved plan that has not been spent?

    THIS IS THE AUTHORISATION QUESTION. An open cycle is what lets the
    agent commit and submit; a spent one is why `submit_local_diff` refuses
    until a fresh plan is approved. Asking it in one place means the gate
    and the plan path can never disagree about which cycle we are in.
    """
    return bool(await pool.fetchval(
        "SELECT 1 FROM plan_versions WHERE story_id = $1 AND status = 'approved' "
        "   AND COALESCE(kind, 'work') = 'work' LIMIT 1", story_id))


async def cycle_plan_path(pool, story_id: str) -> str:
    """`agentos_plans/<story_id>/plan-NN.md` for the cycle now open.

    The kernel's own convention, so a story planned in an editor and the
    same story planned in a pod leave their record in the same place.
    """
    return "{}/{}/plan-{:02d}.md".format(
        PLAN_FOLDER, story_id, await next_cycle(pool, story_id))


async def spend_cycle(pool, story_id: str, event: str) -> int:
    """Close the open cycle, naming what closed it. Returns rows spent.

    IDEMPOTENT BY CONSTRUCTION: it only touches rows still `approved`, so
    a second call after the same event is a no-op rather than a second
    state change. Both callers are retried paths -- a submit can be
    re-posted, Mark Done can be clicked twice -- and neither has a lock.
    """
    spent = await pool.execute(
        "UPDATE plan_versions SET status = 'executed' "
        " WHERE story_id = $1 AND status = 'approved'", story_id)
    n = _rowcount(spent)
    if n:
        logger.info("local cycle spent story=%s by=%s rows=%s", story_id, event, n)
    return n


async def cycle_state(pool, story_id: str) -> dict:
    """What `get-state` reports, so the panel and the agent agree.

    `status` is the cycle's, not the story's: `open` means there is work
    authorised to do, `spent` means the last intent was delivered and
    anything further needs a new plan, `none` means nothing has been
    approved yet.
    """
    row = await pool.fetchrow(
        "SELECT COALESCE(cycle, version) AS cycle, plan_doc_path, status, "
        "       COALESCE(kind, 'work') AS kind FROM plan_versions "
        " WHERE story_id = $1 AND COALESCE(kind, 'work') = 'work' "
        " ORDER BY COALESCE(cycle, version) DESC, version DESC LIMIT 1", story_id)
    # THE CHECKS COME WITH IT, and that is not a convenience.
    #
    # The extension RUNS the cycle's checks and the control plane GATES on
    # them, so the two must be reading one list. `pending_plan.checks` was
    # the only place they were published and it is null the moment a plan
    # is approved -- which is every moment that matters, because the
    # submit happens after approval. For one build the gate therefore
    # refused with `checks_not_passed` naming checks the extension had no
    # way to learn about: correct, and unanswerable.
    #
    # Read through `local_checks.required`, which is the same function the
    # gate asks, so a disagreement between what is run and what is
    # required is not expressible.
    from app.services.local_checks import required  # noqa: PLC0415
    checks = await required(pool, story_id)
    if row is None:
        return {"number": 1, "plan_path": "", "status": "none", "kind": WORK,
                "checks": checks}
    return {
        "number": int(row["cycle"] or 1),
        "plan_path": row["plan_doc_path"] or "",
        "status": "open" if row["status"] == "approved" else "spent",
        "kind": row["kind"],
        "checks": checks,
    }


async def has_any_plan(pool, story_id: str) -> bool:
    return bool(await pool.fetchval(
        "SELECT 1 FROM plan_versions WHERE story_id = $1 "
        "   AND COALESCE(kind, 'work') = 'work' LIMIT 1", story_id))


def _rowcount(tag) -> int:
    """asyncpg returns the command tag; the fakes return an int or None."""
    if isinstance(tag, int):
        return tag
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0
