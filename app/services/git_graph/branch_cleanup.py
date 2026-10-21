"""A merged branch is deleted — but only one the engine cut itself.

`github_ops.delete_ref` has existed since the beginning and its own
docstring says it is "called only for a branch the engine itself cut,
and only after its merge landed on trunk". Nothing ever called it. Every
feature branch this system has ever merged is still sitting on the
remote, and `git branch -r` on the test repo lists two hundred of them.

WHY THIS IS NARROW ON PURPOSE. Deleting a ref is the one thing here that
a person cannot undo from the console, so the guards are the design:

  * ONLY `feat/…` and `ds_…`. These are the two shapes the engine cuts.
    A branch it did not create is not its business, however merged it
    looks — somebody else made it and may still want it.
  * NEVER a trunk, and never the repo's default branch. Deleting the
    thing everything else merges into is not recoverable by re-running
    anything.
  * NOT IF ANOTHER LIVE NODE IS ON IT. A branch named after a dependency
    is a SHARED WORKSTREAM: several nodes ride it, and removing it
    because one of them landed strands every co-member. This is the same
    check the teardown path learned to make, for the same reason.
  * ONLY AFTER the node is MERGED, which the caller has already
    committed — the commits are on trunk before the branch goes.

FAILURE IS NOT AN ERROR. A branch that will not delete costs a little
clutter; a merge finalisation rolled back because of one is a stuck
lane. So this never raises and never runs inside the caller's
transaction. `delete_ref` already treats 404 and 422 as success, which
covers the repo having "automatically delete head branches" switched
on — in which case GitHub got there first and there is nothing to do.
"""

from __future__ import annotations

import logging

from . import github_ops as gh

_log = logging.getLogger(__name__)

# The engine's own shapes. Everything else belongs to somebody.
ENGINE_PREFIXES = ("feat/", "ds_")

# Deleting a ref needs write; detection runs read-only.
_DELETE_PERMS = {"contents": "write", "metadata": "read"}


def is_engine_branch(branch: str) -> bool:
    return any((branch or "").startswith(p) for p in ENGINE_PREFIXES)


# F1 — RECORDING THE OUTCOME lives in `branch_cleanup_state`, with the
# retry that reads it. Both halves in one module is what let "deleted"
# and "failed to delete" be the same row for the whole build.
from .branch_cleanup_state import record as _record       # noqa: E402


async def _story_unfinished(pool, node_id: str) -> bool:
    """Does this node belong to a story that has not finished?

    THE MERGE IS NOT THE END OF THE STORY, in either lane. Abhishek,
    2026-10-14: "the branches have to stay alive until the story is
    marked done."

    Between the merge and `done` the branch is still load-bearing. More
    work pushes to THIS branch -- `kernel_more_work` in the cloud, a new
    cycle locally -- a re-import clones it, and the closeout review
    reads the tree it produced. Deleting at the merge takes it out from
    under all three, and the first symptom is a clone that fails for a
    branch that was worked and landed an hour ago.

    `done`, `reviewed`, `cancelled` and `rejected` are the finished
    states; anything short of them means somebody may still need it.
    A node with NO story is nobody's and is left to the ordinary rules.
    """
    return bool(await pool.fetchval(
        """
        SELECT 1 FROM git_node_stories gns
          JOIN stories s ON s.id = gns.story_id
         WHERE gns.git_node_id = $1
           AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
         LIMIT 1
        """, node_id))


async def _shared_with_live_node(conn, node_id: str, repo_id: str,
                                 branch: str) -> bool:
    """Is another node that has NOT landed still riding this branch?

    The lesson is expensive and was learned on a real teardown: a branch
    named after a dependency carries several nodes, and one of them
    reaching MERGED says nothing about the others.
    """
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM git_nodes
         WHERE git_repo_id = $1 AND branch_name = $2 AND id <> $3
           AND state NOT IN ('MERGED', 'ABANDONED')
         LIMIT 1
        """, repo_id, branch, node_id))


async def delete_merged_branch(pool, node_id: str) -> str:
    """Delete the branch of a just-merged node. Returns what it did, for the log.

    Call AFTER the finalising transaction has committed. Never raises.
    """
    try:
        # COLUMN NAMES READ FROM THE SCHEMA, not guessed. The first
        # version used `repo_id`, `repo_owner` and `repo_name` -- all
        # three wrong (`git_repo_id`, `owner`, `name`) -- so every
        # cleanup died on "column n.repo_id does not exist" and returned
        # the error string. The merge still finalised, which is the
        # design working, and the branch still survived, which is the
        # bug: it looked exactly like the read-only-token failure it had
        # just replaced.
        row = await pool.fetchrow(
            """
            SELECT n.branch_name, n.trunk_branch, n.git_repo_id, n.state,
                   r.owner AS repo_owner, r.name AS repo_name,
                   r.trunk_branch AS repo_trunk
              FROM git_nodes n JOIN git_repos r ON r.id = n.git_repo_id
             WHERE n.id = $1
            """, node_id)
        if row is None:
            return "no such node"
        branch = row["branch_name"] or ""
        if row["state"] != "MERGED":
            return f"node is {row['state']}, not MERGED"
        if not is_engine_branch(branch):
            # Said at INFO rather than passed over silently: somebody
            # reading the log after a merge should be able to see WHY a
            # branch survived.
            _log.info("branch cleanup: %s is not an engine branch; left alone",
                      branch)
            return await _record(pool, node_id, "not an engine branch")
        if branch in (row["trunk_branch"], row["repo_trunk"]):
            _log.warning("branch cleanup: %s is a TRUNK; refusing to delete",
                         branch)
            return await _record(pool, node_id, "refused: trunk")
        if await _shared_with_live_node(pool, node_id, row["git_repo_id"],
                                        branch):
            _log.info("branch cleanup: %s is a shared workstream with a live "
                      "node; left alone", branch)
            return await _record(pool, node_id, "shared workstream")
        if await _story_unfinished(pool, node_id):
            # THE MERGE IS NOT THE END OF THE STORY. Between it and
            # `done` the branch is still load-bearing: more work pushes
            # to THIS branch, a re-import clones it, and the closeout
            # review reads the tree it produced.
            #
            # Abhishek, 2026-10-14: "the branches have to stay alive
            # until the story is marked done" -- in BOTH lanes, which is
            # a deliberate widening. The cloud lane has the same window
            # for the same reason; its run ending is not its story
            # ending.
            _log.info("branch cleanup: %s belongs to a story that is not "
                      "finished; left alone", branch)
            return await _record(pool, node_id, "story unfinished")

        owner, name = row["repo_owner"], row["repo_name"]
        token = await gh.mint_repo_token(name, _DELETE_PERMS)
        ok = await gh.delete_ref(token, owner, name, branch)
        if ok:
            _log.info("branch cleanup: deleted %s/%s:%s after merge",
                      owner, name, branch)
            return await _record(pool, node_id, "deleted")
        _log.warning("branch cleanup: GitHub refused to delete %s/%s:%s",
                     owner, name, branch)
        return await _record(pool, node_id, "github refused")
    except Exception as exc:                                  # noqa: BLE001
        # A branch that will not delete is clutter. A merge finalisation
        # that fails because of one is a stuck lane.
        _log.warning("branch cleanup: %s not deleted (%s)", node_id, exc)
        return await _record(pool, node_id, f"error: {type(exc).__name__}")


async def delete_errand_branch(pool, story_id: str) -> str:
    """R2-F4 — an errand's branch goes when the errand is accepted.

    AN ERRAND'S BRANCH IS SCAFFOLDING, not a record. D1 gives an errand
    the same allocation a story gets — its own branch included — so that
    it has somewhere to work; what it never gets is a landing. The
    deliverable is the artifacts, which are durable in the content store
    and catalogued as resources.

    So the branch has no reader after acceptance. Left alone it would
    accumulate one dead ref per errand for ever, and every one of them
    would look like work somebody forgot to merge.

    WHY NOT `delete_merged_branch`. That one refuses anything not
    `MERGED`, which is exactly right for a story and exactly wrong here:
    an errand's node rests at READY by design and will never be MERGED.
    Same safety rails, different precondition.

    Never raises. A branch that survives is untidy; an exception here
    would be a run that cannot finish.
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT n.id, n.branch_name, n.trunk_branch, n.git_repo_id,
                   n.state, r.owner AS repo_owner, r.name AS repo_name,
                   r.trunk_branch AS repo_trunk
              FROM git_nodes n
              JOIN git_repos r ON r.id = n.git_repo_id
              JOIN git_node_stories s ON s.git_node_id = n.id
             WHERE s.story_id = $1
             LIMIT 1
            """, story_id)
        if row is None:
            return "no node"
        node_id = row["id"]
        branch = row["branch_name"] or ""
        if row["state"] == "MERGED":
            # Should be unreachable — an errand never merges — so if it
            # happens, say so loudly rather than deleting a branch whose
            # contents are now on trunk.
            _log.error("errand %s has a MERGED node; NOT deleting %s",
                       story_id, branch)
            return await _record(pool, node_id, "refused: merged")
        if not is_engine_branch(branch):
            _log.info("errand cleanup: %s is not an engine branch; left alone",
                      branch)
            return await _record(pool, node_id, "not an engine branch")
        if branch in (row["trunk_branch"], row["repo_trunk"]):
            # The rail that matters most. An errand may hold trunk
            # WRITABLE under D1, so a mis-derived ref here would delete
            # the branch the whole goal lands on.
            _log.warning("errand cleanup: %s is a TRUNK; refusing to delete",
                         branch)
            return await _record(pool, node_id, "refused: trunk")
        if await _shared_with_live_node(pool, node_id, row["git_repo_id"],
                                        branch):
            _log.info("errand cleanup: %s is shared with a live node; "
                      "left alone", branch)
            return await _record(pool, node_id, "shared workstream")

        owner, name = row["repo_owner"], row["repo_name"]
        token = await gh.mint_repo_token(name, _DELETE_PERMS)

        # WRITE DOWN WHERE IT POINTED, BEFORE DROPPING THE POINTER.
        #
        # A deleted branch is not deleted history: the commits stay
        # reachable by SHA, and `read_resource` already takes an
        # arbitrary `ref`. So the only thing between "what did that
        # errand change" and an answer was that nobody recorded the SHA
        # while there was still a ref to read it from.
        #
        # The ordering is the whole of it. Earlier records a SHA the
        # errand then commits past; later has nothing to read.
        #
        # BEST EFFORT, and deliberately not a reason to keep the branch:
        # this is a convenience for the window after acceptance, and the
        # artifacts remain the deliverable. A cleanup that failed because
        # a bookkeeping read failed would be the tail wagging the dog.
        try:
            sha = await gh.get_ref_sha(token, owner, name, branch)
            if sha:
                await pool.execute(
                    "UPDATE git_nodes SET final_sha = $2, updated_at = now() "
                    " WHERE id = $1 AND final_sha IS NULL", node_id, sha)
        except Exception as exc:                            # noqa: BLE001
            _log.warning("errand cleanup: could not record the head of "
                         "%s/%s:%s (%s)", owner, name, branch, exc)

        ok = await gh.delete_ref(token, owner, name, branch)
        if ok:
            _log.info("errand cleanup: deleted %s/%s:%s — the errand is "
                      "accepted and its artifacts are the deliverable",
                      owner, name, branch)
            # AND THE CATALOGUE ROW, which `cleanup_landed_branch` only
            # archives on MERGE -- and an errand never merges. So every
            # accepted errand left an ACTIVE branch resource pointing at
            # a ref that no longer exists, which reads to anyone opening
            # the resource list as a branch they could still check out.
            try:
                from app.services.git_graph.branches import (
                    cleanup_landed_branch)
                await cleanup_landed_branch(pool, node_id)
            except Exception as exc:                        # noqa: BLE001
                _log.warning("errand cleanup: branch resource for %s not "
                             "archived (%s)", node_id, exc)
            return await _record(pool, node_id, "deleted")
        _log.warning("errand cleanup: GitHub refused to delete %s/%s:%s",
                     owner, name, branch)
        return await _record(pool, node_id, "github refused")
    except Exception as exc:                                # noqa: BLE001
        _log.warning("errand cleanup for %s failed: %s", story_id, exc)
        return f"error: {type(exc).__name__}"
