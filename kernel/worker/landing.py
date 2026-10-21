"""A run has finished its work. Does it owe a LANDING?

THE PROBLEM THIS EXISTS FOR. A coding story reached `done` the moment a
person accepted it -- which is before the git engine has opened its PR,
let alone had it merged. `reclaim` releases the pod and the workspace
claim on the story reaching `done` or `reviewed`, so by the time anyone
could say "actually, one more thing", the tree that would have held the
change was already collected. The person's only route back was a new
story against a cold checkout.

SO THE RUN WAITS, and it waits the way this system already knows how to
wait: parked. A park holds the pod (`PARKED_HOT` demotes to COLD only
when compute genuinely vanishes), keeps the workspace mounted with its
git history and caches intact, and is already what `finish_execution`
produces for the acceptance that just happened. Nothing new holds the
pod open; the existing hold is simply not let go yet.

WHY IT IS A PARK AND NOT A FLAG. Everything downstream -- reclaim, the
`on_story_done` sweep, review routing, dependent stories -- keys on
states that already exist. A flag would need every one of them taught
about it. A park and a non-terminal story state are both already
understood by all of them, so the change is "the story reaches `done`
later" rather than "the story has a new dimension".

WHO ANSWERS IT. Nobody. This is the one park kind no person resolves:
the answer is the merge, and `app/services/story_landing.py` records it
when the last node lands. That is why `landing` is absent from
`HUMAN_ANSWERED_KINDS` -- a card asking a person to confirm what the
engine is already watching for would be a second, disagreeing authority
over the same fact. Phase 3 adds the card that DOES want a person, and
it comes AFTER this one, once the work is actually on trunk.

ERRANDS NEVER OWE A LANDING. An errand's work does not land -- its
branch is scaffolding, `deliver.py` refuses to publish it, and there is
no PR to wait for. Tested on `executions.kind` rather than the story
row's `work_kind`, for the reason `owes_a_review` gives: the run's own
kind is what resolved the profile that actually ran, so it cannot
disagree with what happened.
"""

from __future__ import annotations

import logging
import os

from kernel.story_kinds import COLUMNS, is_engine_git_story

logger = logging.getLogger(__name__)

# The cutover switch, same shape and same reasoning as
# `STORY_REVIEW_IN_POD`: off, every path writes exactly what it wrote
# before, which is what makes this safe to ship dark and turn on for one
# story. `in os.environ` rather than a defaulted read, because B1 bans a
# default nobody can see across kernel/.
_FLAG = "STORY_HOLDS_FOR_MERGE"


def holds_for_merge_enabled() -> bool:
    """The cutover switch, read per call.

    Public because the places that must agree are not all in here: this
    module deciding whether a finished run waits, `story_landing`
    deciding whether a merge has a park to resolve, and the sweep that
    moves the story into `awaiting_merge`. They must agree, or a story
    waits for a merge nothing is watching for.
    """
    if _FLAG not in os.environ:
        return False
    return os.environ[_FLAG].strip().lower() in ("1", "true", "yes", "on")


LIVE_NODE_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM git_node_stories gns
          JOIN git_nodes gn ON gn.id = gns.git_node_id
         WHERE gns.story_id = $1
           AND gn.state NOT IN ('MERGED', 'ABANDONED'))
"""


async def owes_a_landing(conn, execution_id: int) -> bool:
    """Whether this finished run should wait for its work to land.

    The test is "is there something left to land", not "is this a coding
    story". A story whose nodes are all MERGED or ABANDONED has nothing
    to wait for and must finish now -- otherwise accepting a story whose
    branch landed while it was being reviewed would park it for ever on
    a merge that already happened.
    """
    if not holds_for_merge_enabled():
        return False

    row = await conn.fetchrow(
        "SELECT kind, story_id FROM executions WHERE id = $1", execution_id)
    if row is None or not row["story_id"]:
        return False

    # See the module docstring: the RUN's kind, not the story row's.
    if row["kind"] == "errand":
        return False

    story = await conn.fetchrow(
        f"SELECT {', '.join(COLUMNS)} FROM stories WHERE id = $1",
        row["story_id"])
    if story is None or is_engine_git_story(story):
        # The engine's own merge / diff / fix plumbing. A merge story IS
        # the landing; parking one to wait for itself would deadlock the
        # lane it exists to drain.
        return False

    return bool(await conn.fetchval(LIVE_NODE_SQL, row["story_id"]))


async def park_for_landing(conn, execution_id: int) -> bool:
    """Open the `landing` park. Returns whether the run parked.

    REFUSING TO PARK IS NOT AN ERROR HERE, and the caller must treat it
    as "carry on and finish". `ask_question` returns an `Invalid` rather
    than raising -- a duplicate, say, or a malformed ask -- and a run
    that cannot park must still be allowed to end. The alternative is a
    finished run stuck RUNNING because the thing that was supposed to
    stop it failed, which is strictly worse than finishing early.
    """
    from kernel.harness.park_open import open_request

    row = await conn.fetchrow(
        "SELECT generation, story_id FROM executions WHERE id = $1",
        execution_id)
    if row is None:
        return False

    branches = [r["branch_name"] for r in await conn.fetch(
        "SELECT gn.branch_name FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        " WHERE gns.story_id = $1 "
        "   AND gn.state NOT IN ('MERGED', 'ABANDONED') "
        " ORDER BY gn.branch_name", row["story_id"]) if r["branch_name"]]

    result = await open_request(
        conn, execution_id, row["generation"], "landing",
        {"question": ("Waiting for this story's work to land on trunk. "
                      "No answer is needed: the merge resolves this."),
         "branches": branches,
         # The park is not a person's, so it carries no proposal and no
         # default. `reversible` is true in the only sense that matters
         # here -- nothing has been spent by waiting.
         "reversible": True},
        # KEYED ON THE EXECUTION AND ITS GENERATION. The execution alone
        # was wrong: it assumed a run waits to land exactly once, and a
        # run can now wait SEVERAL times -- trunk moves under a parked
        # story, the run is woken to integrate, pushes, and has to wait
        # again. On the execution alone the second ask comes back
        # E_ASK_ANSWERED and the run is left RUNNING with nothing to do
        # and nobody waiting on it.
        #
        # The generation is what a new round actually is: un-parking to
        # integrate bumps it, so the next wait hashes differently while
        # a repeat within one round still dedupes.
        content_hash=f"landing-exec-{execution_id}-g{row['generation']}")

    if not isinstance(result, int):
        logger.warning(
            "execution %s owed a landing but could not park (%s) — "
            "finishing instead of stalling", execution_id,
            getattr(result, "code", result))
        return False

    logger.info("execution %s parked for landing on %s", execution_id,
                ", ".join(branches) or "(no named branch)")
    return True
