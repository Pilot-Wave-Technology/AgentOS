"""Stopping the RUN when a story is cancelled, and nothing else.

WHAT THIS FILE IS NOT, recorded because I nearly wrote it. I set out to
build a cancellation cascade -- detach dependents, delete git nodes,
retire branches -- and every one of those already exists in
`internal_cancel_story`, done better:

  - dependents are BRIDGED, not detached. a->b->c with b cancelled
    becomes a->c: the dependent inherits b's own dependencies and stays
    on the same branch. Plain removal, which is what I planned, would
    orphan c into its own workstream, split the branch and leave a stale
    merge dependency.
  - the git topology is RE-DERIVED afterwards, which GCs the cancelled
    story's PLANNED node. Cancel is pre-cut by invariant, so there is no
    branch to delete -- the reconcile never touches a live one.
  - the merge case, the ledger event, the channel relay and the
    assignee's notification are all there.

WHAT IS ACTUALLY MISSING is that nothing CALLS it -- it is
internal-bearer only with no route and no button -- and that it leaves
any run alone. A `proposed` story has none; an `accepted` one can have a
run sitting in PENDING, ADMITTED or DISPATCHING, and that run keeps its
admission slot until the `abandoned.py` sweep notices. The sweep is a
backstop, not a reason to leave it holding.

So this file is two functions.
"""

from __future__ import annotations

import logging
import uuid

_log = logging.getLogger(__name__)


async def stop_the_run(conn, story_id: str, actor_uid: str = "") -> int:
    """Cancel any non-terminal run of this story. Returns how many.

    THROUGH THE MAILBOX. Writing `state = 'CANCELLED'` into the table
    would skip the machine edge, the lease release, the admission slot
    and the journal, and leave the machine disagreeing with its own log
    -- the reasoning `kernel_goal_teardown` gives for doing it this way
    when a goal is deleted.

    ON ITS OWN KERNEL CONNECTION, and this took a 500 to find. The
    kernel's functions speak UNQUALIFIED names against the `kernel`
    schema -- `submit_proposal` writes `INSERT INTO proposals` -- and the
    control panel's pooled connections do not have `kernel` on their
    search_path. Handed one of those, the insert raises
    `UndefinedTableError: relation "proposals" does not exist`.

    It failed silently for as long as it existed. The `except` below
    caught it, the cancellation carried on, and the run was never asked
    to stop -- invisible because the only stories anybody could cancel
    had no live run to stop, so the insert never ran. Exempting errands
    from the started-story guard made an in-progress cancel reachable
    for the first time, and the first one 500'd: the failed insert left
    the CALLER'S transaction aborted, so everything after it in the same
    transaction failed too.

    `_kernel_conn` is the existing answer to exactly this -- it is what
    `kernel_run_control` and `cancel_goal_runs` use, and why the thread's
    own Cancel button worked while this did not. A separate connection
    also means the mailbox write is outside the caller's transaction,
    which is right here and not merely tolerable: the contract below is
    that a run which cannot be reached must NOT undo the cancellation.

    NEVER RAISES. The story is already cancelled by the time this runs;
    a run that cannot be reached must not undo that, and the sweep will
    catch it.
    """
    kconn = None
    try:
        from kernel.mailbox.api import submit_proposal

        from app.services.kernel_run_control import TERMINAL, _kernel_conn

        kconn = await _kernel_conn()
        rows = await kconn.fetch(
            "SELECT id FROM kernel.executions "
            " WHERE story_id = $1 AND state <> ALL($2::text[])",
            story_id, list(TERMINAL))
        asked = 0
        for r in rows:
            got = await submit_proposal(
                kconn, f"human:{actor_uid or 'system'}", "cancel",
                {"reason": f"Story {story_id} was cancelled."},
                idempotency_key=f"cancel:{r['id']}:{uuid.uuid4().hex[:12]}",
                execution_id=r["id"])
            asked += 1 if isinstance(got, int) else 0
        if asked:
            _log.info("cancel %s: asked %d run(s) to stop", story_id, asked)
        return asked
    except Exception:                                       # noqa: BLE001
        _log.exception("cancel %s: could not stop its run(s)", story_id)
        return 0
    finally:
        if kconn is not None:
            try:
                await kconn.close()
            except Exception:                               # noqa: BLE001
                pass


# A node whose branch was never cut is a row and nothing else -- the
# reconcile prunes it, and there is no ref anywhere to clean up.
NO_BRANCH_YET = ("PLANNED",)


async def tear_down_nodes(conn, story_id: str) -> dict:
    """Retire the git nodes of a cancelled ERRAND. Returns what it did.

    WHY ONLY ERRANDS NEED THIS. `internal_cancel_story` ends by
    re-deriving the topology, and its comment records the invariant that
    made that sufficient: "Cancel is pre-cut, so reconcile only prunes
    PLANNED nodes -- it never touches a live branch." True while a
    started story could not be cancelled at all.

    EXEMPTING ERRANDS BROKE IT. An errand has no branch to strand --
    it never merges, so its ref is dead either way -- which is why the
    exemption is right. But it means a cancel can now arrive AFTER the
    cut, and reconcile will walk past a BRANCH_CREATED node without
    touching it. The result is a live node for a cancelled errand: a ref
    nobody will merge, no cleanup queued, and no row saying why.

    HANDED TO THE CLEANUP PATH THAT ALREADY EXISTS rather than deleting
    a branch here. `branch_cleanup_tries` drives a sweep that deletes
    the ref, retries a GitHub failure and records what it could not do.
    A second deleter would have to learn all of that again.

    NEVER RAISES. The story is already cancelled and the run already
    stopped; a node that cannot be retired is a dead ref, which is worth
    a log line and not worth undoing the cancellation for.
    """
    out: dict = {"pruned": [], "abandoned": [], "left_shared": [],
                 "left_merged": []}
    try:
        nodes = await conn.fetch(
            "SELECT n.id, n.state FROM git_nodes n "
            "  JOIN git_node_stories gns ON gns.git_node_id = n.id "
            " WHERE gns.story_id = $1", story_id)
        for n in nodes:
            # ONE NODE PER STORY IS THE RULE, and this query is what
            # makes it an assertion rather than an assumption. It costs
            # an index lookup; being wrong costs somebody else's branch.
            shared = await conn.fetchval(
                "SELECT count(*) FROM git_node_stories "
                " WHERE git_node_id = $1 AND story_id <> $2",
                n["id"], story_id) or 0
            if shared:
                await conn.execute(
                    "DELETE FROM git_node_stories "
                    " WHERE git_node_id = $1 AND story_id = $2",
                    n["id"], story_id)
                out["left_shared"].append(n["id"])
                _log.warning("cancel %s: node %s has other member "
                             "stories; membership dropped, branch left "
                             "alone", story_id, n["id"])
                continue
            if n["state"] == "MERGED":
                # It landed. Nothing here is the cancellation's to undo,
                # and an errand should never reach this -- worth the log
                # if one does.
                out["left_merged"].append(n["id"])
                _log.warning("cancel %s: node %s is MERGED; an errand "
                             "should never have landed", story_id, n["id"])
                continue
            if n["state"] in NO_BRANCH_YET:
                out["pruned"].append(n["id"])
                continue            # the reconcile prunes it
            await conn.execute(
                "UPDATE git_nodes "
                "   SET state = 'ABANDONED', "
                "       branch_cleanup_tries = "
                "           GREATEST(COALESCE(branch_cleanup_tries, 0), 1), "
                "       last_error = $2, updated_at = now() "
                " WHERE id = $1 AND state <> 'MERGED'",
                n["id"], f"errand {story_id} was cancelled")
            out["abandoned"].append(n["id"])
        if out["abandoned"]:
            _log.info("cancel %s: abandoned %d node(s) and queued their "
                      "branches for deletion", story_id,
                      len(out["abandoned"]))
        return out
    except Exception:                                       # noqa: BLE001
        _log.exception("cancel %s: could not retire its git nodes",
                       story_id)
        return out
