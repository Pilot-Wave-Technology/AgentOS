"""F1 — whether a branch was actually deleted, and retrying when it was not.

THE STATE COLUMN COULD NOT CARRY THIS. An errand's node rests at READY by
design and never reaches MERGED, so a branch that was deleted and a
branch whose deletion got a 502 were the same row. Nothing could list the
second kind, so nothing retried them, and the remote accumulated one dead
ref per accepted errand — each looking like work somebody forgot to
merge.

So the outcome is written down, and "not attempted", "attempted and
failed" and "settled" are three different things:

    branch_deleted_at IS NULL, tries = 0   never asked
    branch_deleted_at IS NULL, tries > 0   asked, failed — retry this
    branch_deleted_at IS NOT NULL          gone, or never ours to delete

WHY A BOUNDED RETRY. A transient 502 deserves another attempt; a branch
protected by a rule we cannot satisfy deserves to stop being retried and
start being a row somebody looks at. Five tries separates them without
needing to classify the error.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# How many times a failing cleanup is retried before it stops being a
# transient and becomes a visible row.
MAX_CLEANUP_TRIES = 5

# Outcomes meaning "this branch is not ours to delete" — a settled
# answer, not something to retry. `shared workstream` is deliberately NOT
# here: the co-rider will land eventually and the branch becomes
# deletable then.
NOT_OURS = ("not an engine branch", "refused: trunk", "refused: merged")

# DEFERRED IS NEITHER SETTLED NOR FAILED, and it needs its own answer.
#
# "The story is not finished" is a reason to come back later, not a
# reason to stop asking (NOT_OURS) and not an error (which spends one
# of MAX_CLEANUP_TRIES). Treated as a failure, a story that takes more
# than five sweep ticks to finish exhausts its budget and its branch
# survives for ever -- which is precisely the clutter this module
# exists to prevent, arrived at by way of the fix that keeps branches
# alive until `done`.
DEFERRED = ("story unfinished", "shared workstream")


async def record(pool, node_id: str, outcome: str) -> str:
    """Write down what happened, so a failure can be found again.

    Never raises: this is bookkeeping attached to an operation whose
    whole contract is that it cannot fail the caller.
    """
    try:
        if outcome == "deleted":
            await pool.execute(
                "UPDATE git_nodes SET branch_deleted_at = now(), "
                "branch_cleanup_error = NULL, updated_at = now() "
                "WHERE id = $1", node_id)
        elif outcome in DEFERRED:
            # Recorded so the sweep can find it, and NOT counted: the
            # answer is "ask again", and asking again is the point.
            await pool.execute(
                "UPDATE git_nodes SET branch_cleanup_error = $2, "
                "updated_at = now() WHERE id = $1", node_id, outcome)
        elif outcome in NOT_OURS:
            # Not a failure and not a deletion. Marking it settled stops
            # the sweep asking about a branch nobody intends to remove.
            await pool.execute(
                "UPDATE git_nodes SET branch_deleted_at = now(), "
                "branch_cleanup_error = $2, updated_at = now() "
                "WHERE id = $1", node_id, outcome)
        else:
            await pool.execute(
                "UPDATE git_nodes SET branch_cleanup_error = $2, "
                "branch_cleanup_tries = branch_cleanup_tries + 1, "
                "updated_at = now() WHERE id = $1", node_id, outcome)
    except Exception as exc:                                # noqa: BLE001
        _log.warning("branch cleanup: could not record %r for %s (%s)",
                     outcome, node_id, exc)
    return outcome


async def pending(pool, limit: int = 20) -> list:
    """Nodes whose branch deletion was attempted and did not happen.

    `tries > 0` is what makes this a RETRY rather than a second cleanup
    path: a node nobody has asked about yet is not this function's
    business, and sweeping those would delete branches the merge path
    deliberately left alone.
    """
    try:
        rows = await pool.fetch(
            """
            SELECT n.id, n.branch_name, n.state, n.branch_cleanup_tries,
                   n.branch_cleanup_error,
                   EXISTS (SELECT 1 FROM git_node_stories s
                            WHERE s.git_node_id = n.id) AS has_story
              FROM git_nodes n
             WHERE n.branch_deleted_at IS NULL
               AND (
                     (n.branch_cleanup_tries > 0
                      AND n.branch_cleanup_tries < $1)
                     -- A DEFERRAL HAS NO TRIES AND STILL NEEDS ASKING
                     -- ABOUT. `tries > 0` was the whole definition of
                     -- "somebody has looked at this", and a branch held
                     -- back on its FIRST look has tries = 0 -- so the
                     -- sweep would never see it again and the branch
                     -- would outlive the story it was waiting for.
                     OR n.branch_cleanup_error = ANY($3::text[])
                   )
             ORDER BY n.updated_at
             LIMIT $2
            """, MAX_CLEANUP_TRIES, limit, list(DEFERRED))
        return list(rows)
    except Exception as exc:                                # noqa: BLE001
        # Almost always the migration not being applied yet on this
        # database. Quiet and empty beats a sweep that raises on a tick.
        _log.warning("branch cleanup retry: cannot list pending (%s)", exc)
        return []


async def retry_pending(pool, limit: int = 20) -> dict:
    """Re-attempt the cleanups that failed. Returns a small tally.

    Runs OUTSIDE any caller transaction and never raises, for the same
    reason the cleanup itself does not: a branch that will not delete is
    clutter, and a sweep that fails because of one is a stuck tick.
    """
    from .branch_cleanup import delete_errand_branch, delete_merged_branch

    tally: dict = {"attempted": 0, "deleted": 0, "still_failing": 0}
    for row in await pending(pool, limit):
        node_id = row["id"]
        tally["attempted"] += 1
        try:
            if row["state"] == "MERGED":
                outcome = await delete_merged_branch(pool, node_id)
            else:
                story = await pool.fetchval(
                    "SELECT story_id FROM git_node_stories "
                    "WHERE git_node_id = $1 ORDER BY seq LIMIT 1", node_id)
                if not story:
                    # No story and not merged: nothing here knows why the
                    # branch exists, so leave it and stop asking.
                    await record(pool, node_id, "no story to clean up for")
                    continue
                outcome = await delete_errand_branch(pool, story)
        except Exception as exc:                            # noqa: BLE001
            _log.warning("branch cleanup retry: %s raised (%s)", node_id, exc)
            outcome = f"error: {type(exc).__name__}"
            await record(pool, node_id, outcome)
        if outcome == "deleted":
            tally["deleted"] += 1
        else:
            tally["still_failing"] += 1
    if tally["attempted"]:
        _log.info("branch cleanup retry: %s", tally)
    return tally


async def exhausted(pool, limit: int = 50) -> list:
    """Branches that failed every retry — the ones worth a person's time.

    The whole point of counting tries is that this list exists and is
    short. Without it a permanently undeletable branch is either retried
    for ever or forgotten, and both were the previous behaviour.
    """
    try:
        return list(await pool.fetch(
            """
            SELECT n.id, n.branch_name, n.branch_cleanup_error,
                   n.branch_cleanup_tries, r.owner, r.name AS repo
              FROM git_nodes n JOIN git_repos r ON r.id = n.git_repo_id
             WHERE n.branch_deleted_at IS NULL
               AND n.branch_cleanup_tries >= $1
             ORDER BY n.updated_at DESC
             LIMIT $2
            """, MAX_CLEANUP_TRIES, limit))
    except Exception as exc:                                # noqa: BLE001
        _log.warning("branch cleanup: cannot list exhausted (%s)", exc)
        return []
