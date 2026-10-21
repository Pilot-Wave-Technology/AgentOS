"""A run has finished its work. Does it owe a review?

FOUR PATHS MEAN THE SAME THING, and before this they each wrote
`state = 'DONE'` themselves:

    park_resolve.py     a person approved the acceptance park
    loop.py             a standing grant covered the plan — no person at all
    accept.py           a person accepted a REFUSED story, with a reason
    finish_pass.py      the Verifier finished it

Each is "the story is finished", and each now has to answer one more
question before writing anything. Four copies of that answer is four
chances to drift, and the drift would be invisible: a path that forgot
the review branch would finish its stories silently unreviewed, which is
indistinguishable from a story that was reviewed and approved.

So the decision lives here, the four sites call it, and each keeps its
own guard and its own reaction to not moving — `accept.py` raises when
it loses the race, the others do not, and that difference is real.

WHAT IT DECIDES. A run that owes a review does NOT go terminal: it stays
RUNNING with `phase = 'review'` and a fresh generation, and the next leg
resolves the review profile — a surface with no tool that can write —
in the same pod, on the same workspace, with no memory of the work. The
run that owes nothing goes DONE exactly as it did before.

WHY THE POD IS STILL THERE. The story parked hot for acceptance and a
hot park holds its pod; the only demotion to PARKED_COLD is "compute
vanished". So the workspace the reviewer needs is the one the executor
just used, still mounted, with its git history intact. That is the whole
reason the review moved in here.
"""

from __future__ import annotations

import logging
import os

from kernel.story_kinds import COLUMNS, is_engine_git_story

logger = logging.getLogger(__name__)

REVIEW_PHASE = "review"

# The cutover switch. Off, every path below writes DONE exactly as it
# did before, which is what makes this safe to ship dark and turn on for
# one story.
#
# `in os.environ` rather than `.get(name, default)`: B1 bans a defaulted
# read across kernel/, because a default nobody can see is a policy
# nobody reviewed.
_FLAG = "STORY_REVIEW_IN_POD"


def in_pod_review_enabled() -> bool:
    """The cutover switch, read per call.

    Public because three places need the same answer: this
    module deciding whether a finished run owes a review,
    `go_wake_events` deciding whether to route an excluded
    kind to `reviewed`, and `council.runs` deciding whether
    to stand down from the final review. They must agree, or
    a story is either reviewed twice or not at all.
    """
    if _FLAG not in os.environ:
        return False
    return os.environ[_FLAG].strip().lower() in ("1", "true", "yes", "on")


async def owes_a_review(conn, execution_id: int) -> bool:
    """Whether this finished run should review itself before going terminal.

    Three kinds are excluded, and they reach `reviewed` by a different
    road — immediately, with the reason recorded, because a story that
    got there without a review and one that earned it are different
    facts:

        errand        the deliverable is an artifact, already measured
                      mechanically by `propose_completion`. Its branch is
                      scaffolding and goes at `done`; by the time any
                      reviewer ran there would be nothing to read.
        merge story   somebody else's reviewed work, rolled up.
        diff / fix    the engine's own plumbing.
    """
    if not in_pod_review_enabled():
        return False

    row = await conn.fetchrow(
        "SELECT kind, domain, story_id FROM executions WHERE id = $1",
        execution_id)
    if row is None or not row["story_id"]:
        return False

    # THE RUN'S OWN KIND, not the story row's `work_kind`. The two can
    # disagree; `executions.kind` is what resolved the profile that
    # actually ran, so it "cannot disagree with what happened" —
    # `go_wake_events` chose it for the same reason when deciding
    # whether to delete an errand's branch.
    if row["kind"] == "errand":
        return False

    story = await conn.fetchrow(
        f"SELECT {', '.join(COLUMNS)} FROM stories WHERE id = $1",
        row["story_id"])
    if story is not None and is_engine_git_story(story):
        return False

    # FAIL CLOSED ON A MISSING PROFILE. Without this a deployment whose
    # profile tree lacks the review directory would set `phase='review'`,
    # and the next leg would raise `UnknownProfileError` — after the run
    # had already been told not to finish. The story would sit in a
    # phase nothing could serve. Better to finish unreviewed, loudly.
    from kernel.profiles.record import UnknownProfileError
    from kernel.profiles.registry import resolve

    try:
        resolve(row["kind"], row["domain"], REVIEW_PHASE)
    except UnknownProfileError:
        logger.warning(
            "execution %s would owe a review but no %s/%s/%s profile "
            "exists — finishing unreviewed", execution_id,
            row["kind"], row["domain"], REVIEW_PHASE)
        return False
    return True


async def finish_execution(conn, execution_id: int, *,
                           only_from: tuple[str, ...]) -> bool:
    """End the work. Returns whether the row moved.

    `only_from` is the caller's own guard, kept because the four sites
    genuinely differ: acceptance moves out of a park, the autonomous
    path out of RUNNING or a hot park, the Verifier out of VERIFYING,
    and `accept.py` out of exactly the state it validated against — and
    that one RAISES when it does not move, because losing that race
    means a person decided against a run that had already changed.

    `verify_error_count = 0` and `updated_at = now()` are set on every
    path. Three of the four already zeroed the counter and the fourth
    touched `updated_at`; setting both everywhere is inert where it was
    not previously done — the counter is meaningless on a finished run,
    and `updated_at` is what the sweep that fires `on_story_done` reads.
    """
    # ── DOES IT OWE A LANDING? ASKED FIRST, AND THAT ORDER IS THE
    #    DESIGN ─────────────────────────────────────────────────
    #
    # Review reads the work; the merge decides whether the work
    # exists on trunk at all. Reviewing first would mean reviewing a
    # diff that may still change -- the person can ask for one more
    # thing right up until the PR merges -- and would spend the
    # review on a version nobody ends up with.
    #
    # IT IS NOT A THIRD BRANCH OF THE SAME CHOICE. The landing park
    # DEFERS this decision rather than replacing it: when the merge
    # lands, `story_landing` calls this function again, `owes_a
    # _landing` is then false because nothing is left unmerged, and
    # the review-or-DONE choice below happens exactly as it would
    # have. One decision, taken once, later.
    from kernel.worker.landing import owes_a_landing, park_for_landing

    if await owes_a_landing(conn, execution_id):
        # The guard is the caller's, as below: a run that has moved
        # out from under us must not be parked on top of whatever
        # moved it.
        held = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM executions WHERE id = $1 "
            "AND state = ANY($2::text[]))", execution_id, list(only_from))
        if held and await park_for_landing(conn, execution_id):
            logger.info("execution %s finished -> waiting to land",
                        execution_id)
            return True

    review = await owes_a_review(conn, execution_id)
    if review:
        # A FRESH GENERATION, AND A MARK FOR WHERE THE PHASE BEGAN.
        # `_legs_taken` counts generations for the whole execution, so
        # without the mark a reviewer inherits the executor's leg count
        # and the ceiling fires on its first leg — or before it.
        moved = await conn.execute(
            "UPDATE executions "
            "SET state = 'RUNNING', phase = $2, "
            "    generation = generation + 1, "
            "    phase_generation = generation + 1, "
            "    verify_error_count = 0, updated_at = now() "
            "WHERE id = $1 AND state = ANY($3::text[])",
            execution_id, REVIEW_PHASE, list(only_from))
    else:
        moved = await conn.execute(
            "UPDATE executions "
            "SET state = 'DONE', verify_error_count = 0, updated_at = now() "
            "WHERE id = $1 AND state = ANY($2::text[])",
            execution_id, list(only_from))

    ok = moved.split()[-1] != "0"
    if ok:
        logger.info("execution %s finished -> %s", execution_id,
                    "review phase" if review else "DONE")
    return ok
