"""Merge lane (plan §5.3) — drain the per-repo merge queue and land approved nodes.

Single-leader, one lane per repo under an in-process repo lock (§5.0). For the
queue head (FIFO by enqueue_seq):

  1. recovery: a node left in MERGING by a crash → verify-by-read via get_pr
     (merged → forward; else re-attempt).
  2. live-read trunk; `compare` — if the branch is not ahead, it's a no-op land
     (mark MERGED).
  3. re-sync trunk INTO the branch remotely (merge_ref_into) so the PR is current;
     a conflict here → park CONFLICTED.
  4. open/find the PR; read the live branch head sha.
  5. re-assert trunk hasn't moved since the re-sync (pre-PUT TOCTOU, §5.1); if it
     moved → leave QUEUED and stop this pass (retry next tick against new trunk).
  6. durable MERGING (pin pr_number + pre_merge_trunk_sha) → merge_pr.
  7. merged → record merge_commit_sha, advance trunk, mark MERGED, dequeue, and
     mark the merge STORY done when its whole group has landed (§6). head_moved →
     retry; blocked(405) → verify-by-read (merged→forward, else PROTECTION_BLOCKED);
     validation(422) → park.

Each node re-syncs against live trunk when it reaches the head, so a separate
restack-on-merge of siblings is unnecessary for correctness (it's a
conflict-surfacing optimization, deferred). PR ops need a token minted with
pull_requests:write (the GitHub App re-consent prerequisite).

Uses short-lived pool calls (no DB connection held across GitHub I/O).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from app.database import get_pool

from . import github_ops as gh
from .locks import repo_locked
from .repo_identity import set_observed_trunk

_log = logging.getLogger(__name__)

_PR_PERMS = {"contents": "write", "metadata": "read", "pull_requests": "write"}


# States a sibling node can sit in that mean "this merge group has not converged yet"
# (research/MULTI_REPO_STORIES_PLAN.md §15.4). Deliberately the SMALL set of "still
# actively being worked" states, not its complement, so a new state added to the
# CHECK constraint later defaults to NOT blocking rather than silently freezing every
# multi-repo group. PROTECTION_BLOCKED and ESCALATED are included: both mean "this
# node itself still needs a human/engine action before its own state is trustworthy",
# not "already settled" — same treatment merge_lane.py:153/lease.py:76 already give
# ESCALATED elsewhere (counted as "live", not terminal).
_GROUP_NOT_CONVERGED_STATES = (
    "PLANNED", "BRANCHING", "BRANCH_CREATED", "IN_PROGRESS", "PROTECTION_BLOCKED", "ESCALATED",
)


async def enqueue_ready_merges(conn) -> int:
    """Producer step (spec §0/§5/§7): enqueue a node when its work is done (state
    READY) and its merge story is still live. NO pre-PR approval gate — the human
    gate is the GitHub PR merge itself (the Git Engine "Approve" button is retired,
    §0). Transitions READY→QUEUED and inserts the durable queue row. DB-only,
    engine-only.

    Only READY nodes are enqueued; a CONFLICTED (parked) node is NOT auto-re-queued
    here. Idempotent: ON CONFLICT keeps the node's queue row (+ enqueue_seq, the FIFO key).

    (Retired the old `merge_approved_at IS NOT NULL` gate atomically with the
    approve-button retirement — §5 review: repoint enqueue here or the lease goes dark.)

    Multi-repo group-convergence gate (§15.4): a node whose merge GROUP still has a
    sibling in `_GROUP_NOT_CONVERGED_STATES` is held at READY rather than enqueued —
    a repo with no textual conflict of its own can otherwise reach QUEUED (and from
    there open/merge its own PR) while a sibling repo in the SAME multi-write story
    is still being worked, with nobody ever having looked at whether the two changes
    are actually compatible. This is the ONLY chokepoint that needs the check: every
    stage after QUEUED (promote_pending → run_grind_pass → run_detection_pass) is
    strictly per-node/per-lease-slot with no path that re-enters QUEUED any other way
    (the retired self-merge `_drain_repo`/`_recheck_conflicted` path had a second
    re-queue point; it is dead code, not called from `engine._tick`). A single-node
    group (the common single-repo case) has no sibling row at all, so the added
    `NOT EXISTS` is always true for it — byte-identical to before."""
    # COALESCE trunk_branch to the repo default (git_repos.trunk_branch is NOT NULL)
    # so the queue row is NEVER NULL-trunk — a NULL would escape the lease index AND
    # be skipped by promote_pending forever (a silent strand).
    rows = await conn.fetch(
        "UPDATE git_nodes n SET state = 'QUEUED', updated_at = now() "
        "FROM git_merge_group_nodes gmn, stories s, git_repos r "
        "WHERE n.id = gmn.git_node_id AND r.id = n.git_repo_id "
        "  AND s.merge_group_id = gmn.merge_group_id AND s.story_kind = 'merge' "
        "  AND s.state NOT IN ('cancelled', 'rejected') "
        "  AND n.state = 'READY' "
        # D1 — AN ERRAND'S WORK NEVER LANDS IN TRUNK.
        #
        # An errand is allocated what a story is allocated: its own
        # branch, and trunk too when it needs it, writable when the work
        # needs that. It commits, it pushes its own branch, and the
        # result is inspectable afterwards. What it never gets is a
        # LANDING.
        #
        # Enforced HERE because this function's own docstring says it is
        # "the ONLY chokepoint that needs the check" — every stage after
        # QUEUED is per-node with no path back in. A node with any errand
        # member is held at READY for ever, which is the correct resting
        # state: the branch exists, the work is on it, nothing merges.
        #
        # NOT enforced by refusing errands a branch. That was the
        # original design and it produced two defects and an empty
        # workspace panel; the constraint belongs on the destination of
        # the work, not on the work.
        #
        # AND THE RUN, NOT ONLY THE STORY. The assistant may run a story
        # as an errand without the story changing shape, and the wake
        # at DONE reads the RUN: it deletes the errand's branch. Reading
        # only the story here, the engine then queued a merge of that
        # deleted branch -- GitHub 404 on compare, node ESCALATED, a
        # card asking a human to push it back (GOAL2 / e64f5e80). The
        # story's LATEST run decides, so a story re-run as a story lands.
        "  AND NOT EXISTS ( "
        "    SELECT 1 FROM git_node_stories gns "
        "    JOIN stories est ON est.id = gns.story_id "
        "    WHERE gns.git_node_id = n.id AND (est.work_kind = 'errand' "
        "      OR (SELECT e.kind FROM kernel.executions e "
        "           WHERE e.story_id = est.id "
        "           ORDER BY e.id DESC LIMIT 1) = 'errand') "
        "  ) "
        "  AND NOT EXISTS ( "
        "    SELECT 1 FROM git_merge_group_nodes sib_gmn "
        "    JOIN git_nodes sib_n ON sib_n.id = sib_gmn.git_node_id "
        "    WHERE sib_gmn.merge_group_id = gmn.merge_group_id "
        "      AND sib_n.id <> n.id "
        f"      AND sib_n.state = ANY($1::text[]) "
        "  ) "
        "RETURNING n.id AS node_id, n.git_repo_id, n.goal_id, "
        "          COALESCE(n.trunk_branch, r.trunk_branch) AS trunk_branch",
        list(_GROUP_NOT_CONVERGED_STATES),
    )
    # Enqueue in intra-group `seq` order (multi-repo plan M2/L4). Within a merge group,
    # `git_merge_group_nodes.seq` is the declared cross-repo landing order; enqueueing
    # in that order makes each node's FIFO position (enqueue_seq) follow it, so repo A
    # takes its lane slot before repo B when the story declared that ordering.
    #
    # ADVISORY, NOT A GATE: the lease is per (repo, trunk), so nodes in different repos
    # hold different lanes and can still land out of order if a human merges B's PR
    # first — the engine never merges, so it cannot enforce sequencing. Ordering the
    # enqueue is the strongest honest guarantee available here. Nodes are ordered
    # deterministically by (seq, node_id) so a tie never reorders between passes.
    seq_by_node: dict[str, int] = {}
    if rows:
        for sr in await conn.fetch(
            "SELECT git_node_id, min(seq) AS seq FROM git_merge_group_nodes "
            "WHERE git_node_id = ANY($1::text[]) GROUP BY git_node_id",
            [r["node_id"] for r in rows],
        ):
            seq_by_node[sr["git_node_id"]] = sr["seq"] or 0
    rows = sorted(rows, key=lambda r: (seq_by_node.get(r["node_id"], 0), r["node_id"]))

    seen: set[str] = set()
    for r in rows:
        # The UPDATE...FROM join can emit a node more than once (a node in >1 group);
        # the INSERT is idempotent, but dedupe so the returned count is accurate.
        if r["node_id"] in seen:
            continue
        seen.add(r["node_id"])
        # trunk_branch is REQUIRED for the lease index (one-active per repo,trunk) to
        # constrain this row. ON CONFLICT keeps enqueue_seq (the immutable FIFO key).
        await conn.execute(
            "INSERT INTO git_merge_queue (node_id, git_repo_id, goal_id, trunk_branch, status) "
            "VALUES ($1, $2, $3, $4, 'waiting') "
            "ON CONFLICT (node_id) DO UPDATE SET status = 'waiting', "
            "  trunk_branch = EXCLUDED.trunk_branch, enqueued_at = now()",
            r["node_id"], r["git_repo_id"], r["goal_id"], r["trunk_branch"],
        )
    return len(seen)


async def cancel_engine_stories_for_group(executor, merge_group_id: str) -> str:
    """Cancel non-terminal ENGINE-authored diff/fix code stories in a merge group.

    Called whenever a group's workstream is torn down (merge rejected / node abandoned /
    human PR-close) OR landed (a successful merge — the integrate/fix stories are then
    moot): the engine's integrate/fix stories are dispatched to assistants and would
    otherwise keep running on a branch that no longer needs them — the orphaned
    in-progress 'Integrate dev …' / 'Fix …' stories. `executor` may be a pool or a conn.
    Clears active_thread_id so the SHCA watchdog reaps the workspace. Idempotent.

    SCOPE (multi-repo plan M2.4). Merge groups can now hold MORE THAN ONE node (a
    multi-write story contributes one node per repo), so this whole-group form is only
    correct when the whole group has settled. Use it ONLY from `_finalize_merged`,
    which is gated on whole-group-landed (remaining == 0). Per-node callers must use
    `cancel_engine_stories_for_node` instead, or they will cancel a sibling repo's
    still-needed diff/fix story."""
    if not merge_group_id:
        return "UPDATE 0"
    return await executor.execute(
        "UPDATE stories SET state = 'cancelled', active_thread_id = NULL, updated_at = now() "
        "WHERE created_by = 'engine' AND story_kind = 'code' AND merge_group_id = $1 "
        "  AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')",
        merge_group_id,
    )


async def reject_merge_story_if_last_live_node(
    conn, node_id: str, reason: str, *, terminal_state: str = "rejected",
) -> bool:
    """Tear down a node's merge story ONLY when no sibling node is still live.

    Returns True if a merge story was terminalized, False if it was left open because
    a sibling repo is still working.

    WHY (multi-repo plan, review finding): a merge group used to hold exactly one node,
    so "this node was cancelled" and "this group is finished" were the same statement.
    With a multi-write story the group holds one node per repo, and terminalizing the
    shared merge story because ONE repo was cancelled strands every sibling: the merge
    story is the enqueue predicate (`enqueue_ready_merges` skips
    cancelled/rejected merge stories), so the healthy repos are never queued again,
    never open a PR, and never escalate — they simply stop, with no operator-visible
    error and no self-heal path.

    A node is "live" while it is neither MERGED nor ABANDONED. ESCALATED counts as live
    on purpose: it is recoverable through the operator Retry route
    (git_graph.git_retry_node), which resets the node to PLANNED and reactivates a
    rejected merge story.

    When a sibling IS still live the caller should tell somebody — see
    `notify_merge_group_node_cancelled`. Correct-but-silent is the failure mode this
    change would otherwise introduce: the story simply never completes, because
    `_finalize_merged` requires EVERY node to be MERGED.
    """
    terminalized = False
    for row in await conn.fetch(
        "SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = $1",
        node_id,
    ):
        gid = row["merge_group_id"]
        live_siblings = await conn.fetchval(
            "SELECT count(*) FROM git_merge_group_nodes gmn "
            "  JOIN git_nodes n ON n.id = gmn.git_node_id "
            " WHERE gmn.merge_group_id = $1 AND gmn.git_node_id <> $2 "
            "   AND n.state NOT IN ('MERGED', 'ABANDONED')",
            gid, node_id,
        )
        if live_siblings:
            _log.info(
                "git-engine: node %s cancelled but %d live sibling node(s) remain in "
                "group %s — leaving the merge story OPEN so they can still land",
                node_id, live_siblings, gid,
            )
            continue
        await conn.execute(
            "UPDATE stories SET state = $2, updated_at = now() "
            "WHERE story_kind = 'merge' AND state NOT IN ('done', 'rejected', 'cancelled') "
            "  AND merge_group_id = $1",
            gid, terminal_state,
        )
        terminalized = True
    return terminalized


async def notify_merge_group_node_cancelled(
    conn, node_id: str, reason: str,
) -> list[tuple[str, str]]:
    """Post a notice on the merge story's thread when ONE repo of a multi-repo story
    was torn down but the merge story stayed open.

    Without this the operator gets no signal at all: the board still shows the merge
    story as open and the healthy repos keep working, while the story can in fact never
    complete until the cancelled node is retried. Best-effort — a notification failure
    must never break the teardown transaction it runs inside.

    Returns the (thread_id, message_id) pairs it inserted. The caller must hand these to
    `fan_out_cancel_notices` AFTER its transaction commits: the chat fan-out
    (notify_new_message) takes its OWN pool connection, so firing it inside the
    transaction would announce a message to every browser even if the teardown then
    rolled back — and the broadcaster would find no such row.
    """
    inserted: list[tuple[str, str]] = []
    try:
        rows = await conn.fetch(
            "SELECT s.id, s.thread_id, n.branch_name, r.owner, r.name "
            "  FROM git_merge_group_nodes gmn "
            "  JOIN stories s ON s.merge_group_id = gmn.merge_group_id "
            "                AND s.story_kind = 'merge' "
            "  JOIN git_nodes n ON n.id = gmn.git_node_id "
            "  JOIN git_repos r ON r.id = n.git_repo_id "
            " WHERE gmn.git_node_id = $1 "
            "   AND s.state NOT IN ('done', 'cancelled', 'rejected')",
            node_id,
        )
        for row in rows:
            if not row["thread_id"]:
                continue
            body = (
                f"### One repository was cancelled — the rest are still going\n\n"
                f"**`{row['owner']}/{row['name']}`** (branch `{row['branch_name']}`) was "
                f"torn down: {reason[:300]}\n\n"
                f"The other repositories in this change are unaffected and will keep "
                f"working toward their own pull requests. **This story cannot complete "
                f"until this repository is recovered** — every repository has to land.\n\n"
                f"Use **Retry** on the node in the Git panel once the cause is fixed, or "
                f"cancel the remaining repositories if the whole change is being dropped."
            )
            msg_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO messages (id, thread_id, role, sender, content, created_at)
                   VALUES ($1, $2, 'agent', 'git-engine', $3, NOW())""",
                msg_id, row["thread_id"], body,
            )
            inserted.append((row["thread_id"], msg_id))
            _log.info(
                "git-engine: notified merge story %s that node %s was cancelled",
                row["id"], node_id,
            )
    except Exception as exc:
        _log.warning(
            "git-engine: could not post merge-group cancel notice for node %s: %s",
            node_id, exc,
        )
    return inserted


async def fan_out_cancel_notices(pending: list[tuple[str, str]]) -> None:
    """Push the cancel notices to open chat tabs. Call AFTER the teardown transaction
    has COMMITTED — never inside it (see notify_merge_group_node_cancelled).

    Without this the notice exists in the DB but no open tab learns of it until a manual
    refresh, which is the same trap diff_review documents: a card that "only ever
    appeared on refresh". Best-effort; the persisted row is the fallback.
    """
    if not pending:
        return
    try:
        from app.chat_module.repository import notify_new_message
        for thread_id, msg_id in pending:
            await notify_new_message(thread_id, msg_id)
    except Exception as exc:
        _log.warning("git-engine: cancel-notice fan-out failed: %s", exc)


async def cancel_engine_stories_for_node(
    executor, node_id: str, merge_group_id: str = "",
) -> str:
    """Cancel non-terminal ENGINE diff/fix stories belonging to ONE node.

    The per-node counterpart of `cancel_engine_stories_for_group`, for the callers
    that tear down a single node (`_cancel_node`, `reap_deleted_goal_nodes`, the merge
    cancel route). With multi-node merge groups, cancelling by group there would take
    down a sibling repo's in-flight diff/fix story even though that repo is still
    perfectly healthy.

    `merge_group_id` is an optional LEGACY fallback: engine stories written before
    `stories.git_node_id` existed have a NULL node, so they can only be found by
    group. It is applied only when the group is single-node — otherwise the fallback
    would reintroduce exactly the cross-repo clobbering this function exists to stop.
    Clears active_thread_id so the SHCA watchdog reaps the workspace. Idempotent.
    """
    if not node_id:
        return "UPDATE 0"

    legacy_group = ""
    if merge_group_id:
        sibling_count = await executor.fetchval(
            "SELECT count(*) FROM git_merge_group_nodes WHERE merge_group_id = $1",
            merge_group_id,
        )
        if (sibling_count or 0) <= 1:
            legacy_group = merge_group_id

    return await executor.execute(
        "UPDATE stories SET state = 'cancelled', active_thread_id = NULL, updated_at = now() "
        "WHERE created_by = 'engine' AND story_kind = 'code' "
        "  AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected') "
        "  AND (git_node_id = $1 "
        "       OR (git_node_id IS NULL AND $2 <> '' AND merge_group_id = $2))",
        node_id, legacy_group,
    )


async def _refresh_protection(
    token: str, owner: str, name: str, trunk_branch: str, repo_id: str,
) -> dict:
    """Read the repo's merge settings + trunk protection just-in-time and update
    git_repos (plan §9). Returns the merge_method the lane should use. merge_method
    respects linear_history (the git_repos CHECK forbids merge_method='merge' with
    linear_history). merge_queue_enabled is NOT auto-detected here (REST can't
    cleanly; GraphQL deferred) — it stays whatever git_repos holds (admin-settable)."""
    pool = get_pool()
    try:
        settings = await gh.get_repo_settings(token, owner, name)
        prot = await gh.get_branch_protection(token, owner, name, trunk_branch)
    except Exception:
        _log.exception("git-engine: protection read failed for repo %s", repo_id)
        row = await pool.fetchrow("SELECT merge_method FROM git_repos WHERE id = $1", repo_id)
        return {"merge_method": (row["merge_method"] if row else "merge")}

    linear = prot["linear_history"]
    if settings["allow_merge"] and not linear:
        method = "merge"
    elif settings["allow_squash"]:
        method = "squash"
    elif settings["allow_rebase"]:
        method = "rebase"
    else:
        method = "squash"  # fallback if a repo allows none of the three (unusual)
    await pool.execute(
        "UPDATE git_repos SET merge_method = $2, linear_history = $3, protected = $4, "
        "  required_checks = $5::jsonb, required_reviews = $6, updated_at = now() WHERE id = $1",
        repo_id, method, linear, prot["protected"],
        json.dumps(prot["required_checks"]), prot["required_reviews"],
    )
    return {"merge_method": method}


async def run_merge_pass(goal_id: Optional[str] = None) -> dict:
    """RETIRED (merge-model rework, spec §10) — NO LONGER WIRED into `engine._tick`.

    This is the engine self-merge LAND (it calls `merge_pr` / `PUT /merge`), which is
    incompatible with the human-merge model: a human merges on the PR and
    `detect.run_detection_pass` reconciles. Do NOT re-wire this. It (and
    `_drain_repo`/`_merge_one`) will be deleted when the queue/lease lands (build §5);
    `_finalize_merged` / `_park` / `enqueue_ready_merges` are KEPT (reused by detection
    + the queue). Left in place only so build §5 can lift the parts it reuses.

    Drain every repo that has waiting queue entries. Each repo under its own
    in-process lane lock so different repos merge in parallel within the leader."""
    pool = get_pool()
    # Repos with a waiting queue head OR a parked CONFLICTED+approved node (the
    # latter so a repo whose only work is a resolved conflict still gets a lane).
    g = " AND goal_id = $1" if goal_id else ""
    gn = " AND n.goal_id = $1" if goal_id else ""
    rows = await pool.fetch(
        # sql: safe -- `g` and `gn` are literal ternaries two lines up,
        # each either "" or a fragment binding $1.
        f"SELECT DISTINCT git_repo_id FROM git_merge_queue WHERE status = 'waiting'{g} "
        "UNION "
        "SELECT DISTINCT n.git_repo_id FROM git_nodes n "
        "  JOIN git_merge_group_nodes gmn ON gmn.git_node_id = n.id "
        "  JOIN stories s ON s.merge_group_id = gmn.merge_group_id AND s.story_kind = 'merge' "
        f" WHERE n.state = 'CONFLICTED' AND s.merge_approved_at IS NOT NULL{gn}",
        *((goal_id,) if goal_id else ()),
    )
    totals = {"merged": 0, "parked": 0, "blocked": 0, "skipped": 0, "requeued": 0}
    for r in rows:
        repo_id = r["git_repo_id"]
        async with repo_locked(repo_id):
            res = await _drain_repo(repo_id)
        for k in totals:
            totals[k] += res.get(k, 0)
    return totals


async def _drain_repo(repo_id: str) -> dict:
    pool = get_pool()
    out = {"merged": 0, "parked": 0, "blocked": 0, "skipped": 0, "requeued": 0}
    repo = await pool.fetchrow(
        "SELECT owner, name, trunk_branch, merge_queue_enabled "
        "FROM git_repos WHERE id = $1",
        repo_id,
    )
    if repo is None:
        return out
    if repo["merge_queue_enabled"]:
        # Native GitHub merge queue (GraphQL enqueuePullRequest) is a later
        # concern (plan §5.2/§5.3). Don't drive our own Bors loop for such repos.
        _log.warning(
            "git-engine: repo %s has merge_queue_enabled — native-queue path not "
            "built; leaving its queue untouched", repo_id,
        )
        return out

    owner, name = repo["owner"], repo["name"]
    # PER-GOAL-TRUNK TRAP (build §5 must fix when re-wiring): this reads the repo's
    # CANONICAL trunk_branch, which is last-write-wins across goals (§0). The LIVE
    # finalize path (detect.py) correctly uses the NODE's trunk_branch. When build §5
    # lifts this lane's reused parts into the queue, thread node.trunk_branch through
    # here too (and per-(repo,trunk) protection), or the per-goal-trunk bug returns.
    trunk_branch = repo["trunk_branch"] or "main"
    try:
        token = await gh.mint_repo_token(name, _PR_PERMS)
    except Exception:
        _log.exception("git-engine: token mint failed for repo %s", repo_id)
        return out

    # Refresh merge settings + trunk protection just-in-time (plan §9); the
    # returned merge_method respects linear_history.
    cfg = await _refresh_protection(token, owner, name, trunk_branch, repo_id)
    merge_method = cfg["merge_method"]

    # Re-check parked CONFLICTED nodes whose conflict the assistant has resolved
    # (the branch now contains trunk) and re-queue them before draining.
    try:
        out["requeued"] = await _recheck_conflicted(token, owner, name, trunk_branch, repo_id)
    except Exception:
        _log.exception("git-engine: conflict re-check failed for repo %s", repo_id)

    # Drain one head at a time. A 'retry'/'skip' outcome stops this pass (no tight
    # loop on the same head); 'merged'/'parked'/'blocked' change the queue status
    # so the next head differs.
    while True:
        head = await pool.fetchrow(
            "SELECT q.node_id, n.branch_name, n.state, n.pr_number, n.pre_merge_trunk_sha "
            "FROM git_merge_queue q JOIN git_nodes n ON n.id = q.node_id "
            "WHERE q.git_repo_id = $1 AND q.status = 'waiting' "
            "ORDER BY q.enqueue_seq ASC LIMIT 1",
            repo_id,
        )
        if head is None:
            break
        try:
            outcome = await _merge_one(
                token, owner, name, trunk_branch, merge_method, repo_id, dict(head)
            )
        except Exception:
            _log.exception("git-engine: merge attempt failed for node %s", head["node_id"])
            break  # stop this repo's pass; retry next tick
        if outcome in ("merged", "parked", "blocked"):
            out[outcome] += 1
            continue
        # 'retry' / 'skip' — leave it queued, stop draining this repo this pass.
        out["skipped"] += 1
        break
    return out


async def _merge_one(
    token: str, owner: str, name: str, trunk_branch: str, merge_method: str,
    repo_id: str, head: dict,
) -> str:
    pool = get_pool()
    node_id = head["node_id"]
    branch = head["branch_name"]
    state = head["state"]

    # 1. Recovery: a crash left this node in MERGING. Resolve forward by reading
    #    the PR's authoritative state (verify-by-read, §5.1).
    if state == "MERGING" and head.get("pr_number"):
        pr = await gh.get_pr(token, owner, name, head["pr_number"])
        if pr["merged"]:
            await _finalize_merged(repo_id, node_id, pr["merge_commit_sha"] or "",
                                   token, owner, name, trunk_branch)
            return "merged"
        # not merged → fall through and re-attempt the land below.

    # 2. Live trunk + ahead check.
    trunk_sha = await gh.get_trunk_sha(token, owner, name, trunk_branch)
    cmp = await gh.compare(token, owner, name, trunk_sha, branch)
    if cmp["ahead_by"] == 0:
        # Branch has nothing trunk doesn't already have → no-op land.
        await _finalize_merged(repo_id, node_id, trunk_sha,
                               token, owner, name, trunk_branch)
        return "merged"

    # 3. Re-sync trunk INTO the branch so the PR is current/mergeable.
    sync = await gh.merge_ref_into(token, owner, name, branch, trunk_sha)
    if sync["status"] == "conflict":
        await _park(node_id, trunk_sha, "merge conflict re-syncing trunk into branch")
        return "parked"
    if sync["status"] == "missing":
        await _park(node_id, trunk_sha, "branch or trunk ref missing during re-sync")
        return "parked"

    # 4. Open/find the PR + the live branch head sha (post re-sync).
    pr = await gh.open_or_get_pr(
        token, owner, name, branch, trunk_branch, f"Merge {branch} → {trunk_branch}"
    )
    pr_number = pr["number"]
    head_sha = await gh.get_ref_sha(token, owner, name, branch) or pr.get("head_sha", "")

    # 5. Pre-PUT TOCTOU: re-assert trunk hasn't moved since the re-sync (§5.1). An
    #    external push between here and the merge would land CI-unverified content.
    trunk_now = await gh.get_trunk_sha(token, owner, name, trunk_branch)
    if trunk_now != trunk_sha:
        return "retry"  # trunk moved — re-loop next tick against the new trunk

    # 6. Durable MERGING (pin pr_number + the base we restacked/verified against).
    claimed = await pool.fetchrow(
        "UPDATE git_nodes SET state = 'MERGING', pr_number = $2, "
        "  pre_merge_trunk_sha = $3, updated_at = now() "
        "WHERE id = $1 AND state IN ('QUEUED', 'MERGEABLE', 'READY', 'MERGING') "
        "RETURNING id",
        node_id, pr_number, trunk_sha,
    )
    if claimed is None:
        return "skip"  # node advanced out from under us

    # 7. Merge (perform-once + verify-by-read).
    m = await gh.merge_pr(token, owner, name, pr_number, head_sha, merge_method)
    if m["status"] == "merged":
        await _finalize_merged(repo_id, node_id, m.get("sha") or "",
                               token, owner, name, trunk_branch)
        return "merged"
    if m["status"] == "head_moved":
        return "retry"  # branch head changed — re-sync next tick
    if m["status"] == "blocked":
        pr2 = await gh.get_pr(token, owner, name, pr_number)
        if pr2["merged"]:
            await _finalize_merged(repo_id, node_id, pr2["merge_commit_sha"] or "",
                                   token, owner, name, trunk_branch)
            return "merged"
        await _protection_blocked(node_id, m.get("detail", "not mergeable"))
        return "blocked"
    # validation (422) — genuine failure; park for human attention.
    await _park(node_id, trunk_sha, f"merge validation failed: {m.get('detail', '')}")
    return "parked"


async def _recheck_conflicted(
    token: str, owner: str, name: str, trunk_branch: str, repo_id: str,
) -> int:
    """Re-queue parked CONFLICTED nodes whose conflict has been resolved (plan §7).

    A node is parked CONFLICTED when trunk couldn't merge into its branch. The
    merge story's assistant resolves it in the workspace (syncBranch → resolve →
    gitCommit pushes), after which the branch CONTAINS the (then-current) trunk.
    Here we live-read trunk and `compare`: if the branch is no longer behind trunk
    (behind_by == 0) the conflict is resolved against current trunk → re-queue
    (CONFLICTED → QUEUED). If trunk advanced again since resolution, behind_by > 0
    and it stays CONFLICTED (the assistant must re-sync).

    Only CONFLICTED + approved nodes are checked (bounded; conflicts are rare). A
    smarter trigger (NOTIFY on the resolution push) is a rate-limit optimization
    deferred to §9."""
    pool = get_pool()
    nodes = await pool.fetch(
        "SELECT n.id, n.branch_name FROM git_nodes n "
        "JOIN git_merge_group_nodes gmn ON gmn.git_node_id = n.id "
        "JOIN stories s ON s.merge_group_id = gmn.merge_group_id AND s.story_kind = 'merge' "
        "WHERE n.git_repo_id = $1 AND n.state = 'CONFLICTED' "
        "  AND s.merge_approved_at IS NOT NULL",
        repo_id,
    )
    requeued = 0
    seen: set[str] = set()
    for n in nodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        try:
            trunk_sha = await gh.get_trunk_sha(token, owner, name, trunk_branch)
            cmp = await gh.compare(token, owner, name, trunk_sha, n["branch_name"])
        except Exception:
            _log.exception("git-engine: conflict re-check compare failed node %s", n["id"])
            continue
        if cmp["behind_by"] != 0:
            continue  # branch still missing trunk commits → not yet resolved
        moved = await pool.fetchrow(
            "UPDATE git_nodes SET state = 'QUEUED', mergeable = 'unknown', "
            "  conflict_paths = NULL, updated_at = now() "
            "WHERE id = $1 AND state = 'CONFLICTED' RETURNING git_repo_id, goal_id, trunk_branch",
            n["id"],
        )
        if moved:
            await pool.execute(
                "INSERT INTO git_merge_queue (node_id, git_repo_id, goal_id, trunk_branch, status) "
                "VALUES ($1, $2, $3, $4, 'waiting') "
                "ON CONFLICT (node_id) DO UPDATE SET status = 'waiting', "
                "  trunk_branch = EXCLUDED.trunk_branch, enqueued_at = now()",
                n["id"], moved["git_repo_id"], moved["goal_id"], moved["trunk_branch"],
            )
            requeued += 1
    return requeued


async def _finalize_merged(
    repo_id: str, node_id: str, new_trunk_sha: str,
    token: str, owner: str, name: str, trunk_branch: str,
) -> None:
    """Node MERGED → advance the observed trunk cache, dequeue, and mark the merge
    STORY done when its whole group has landed (§6). If the API returned no
    merge_commit_sha (it can be briefly null right after a merge), re-read live
    trunk so the observed trunk cache still advances."""
    pool = get_pool()
    if not new_trunk_sha:
        try:
            new_trunk_sha = await gh.get_trunk_sha(token, owner, name, trunk_branch)
        except Exception:
            new_trunk_sha = ""
    # One transaction so a crash can't leave a torn state (e.g. node MERGED but queue
    # still 'active' → the lane is stuck behind a zombie until a leader restart). All
    # mutations — node, trunk caches, queue dequeue, merge-story rollup — commit together.
    async with pool.acquire() as conn, conn.transaction():
        # ABANDONED is excluded (not just MERGED): a dead goal's node is reaped to
        # ABANDONED (reap_deleted_goal_nodes) but its PR may still be open; if a human
        # later merges it, we must NOT revive the node to MERGED or mark its (cancelled)
        # merge story done. ESCALATED is deliberately NOT excluded — an escalated node
        # whose PR a human then merges SHOULD finalize (merged-after-escalate, §6).
        flipped = await conn.fetchval(
            "UPDATE git_nodes SET state = 'MERGED', merge_commit_sha = $2, updated_at = now() "
            "WHERE id = $1 AND state NOT IN ('MERGED', 'ABANDONED') RETURNING id",
            node_id, new_trunk_sha or None,
        )
        if new_trunk_sha:
            # The merge genuinely moved trunk — advance the observed cache for ALL goals
            # on this trunk REGARDLESS of whether we finalized THIS node (Case-6 "advance
            # on ANY merge"). Per-(repo,trunk) cache (§0) keyed by the NODE's trunk_branch,
            # plus the legacy git_repos.trunk_sha (coarse display cache).
            await set_observed_trunk(conn, repo_id, trunk_branch, new_trunk_sha)
            await conn.execute(
                "UPDATE git_repos SET trunk_sha = $2, trunk_sha_seen_at = now() WHERE id = $1",
                repo_id, new_trunk_sha,
            )
        if not flipped:
            # Node was already terminal (idempotent re-finalize of a MERGED node, or a
            # reaped ABANDONED node) — trunk advance above still applied, but don't
            # dequeue / roll up the merge story for a node we didn't actually land.
            return
        await conn.execute(
            "UPDATE git_merge_queue SET status = 'merged' WHERE node_id = $1", node_id
        )
        # A merge story lands a GROUP; mark it done only when EVERY group node MERGED.
        groups = await conn.fetch(
            "SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = $1",
            node_id,
        )
        for g in groups:
            gid = g["merge_group_id"]
            remaining = await conn.fetchval(
                "SELECT count(*) FROM git_merge_group_nodes gmn "
                "JOIN git_nodes n ON n.id = gmn.git_node_id "
                "WHERE gmn.merge_group_id = $1 AND n.state <> 'MERGED'",
                gid,
            )
            if (remaining or 0) == 0:
                from app.services.story_state_ledger import (
                    record_states_where)
                await record_states_where(
                    conn, "done",
                    where=("merge_group_id = $1 AND story_kind = 'merge' "
                           "AND state <> 'done'"),
                    params=(gid,), action="merge_group_landed")
                # The whole group has LANDED → any in-flight engine integrate/fix (diff)
                # stories are now MOOT (the branch they were helping land is merged).
                # Cancel them so they don't orphan in_progress. The CANCEL/reject paths
                # already do this (detect._cancel_node / reap); the MERGE path must too,
                # else a human-merged PR strands its integrate story (observed live:
                # merge story 'done' but "Integrate dev into <branch>" stuck in_progress).
                await cancel_engine_stories_for_group(conn, gid)
        # A story whose every branch has landed has nothing left to discuss.
        #
        # Closed HERE, in the finalize transaction, so the conversation closes
        # exactly when the work lands or not at all — a thread retired against
        # a merge that then rolled back would be a conversation taken away for
        # no reason, and one left open after the last branch landed is the
        # clutter this exists to remove.
        #
        # Per STORY, not per node: `landed_stories_for_node` only closes a
        # story with no live node anywhere, so a multi-repo change keeps its
        # thread through the first two merges and closes on the third. Nothing
        # is deleted — the conversation is the record of how the work was done.
        from .story_threads import close_threads_for_landed
        closed_threads = await close_threads_for_landed(conn, node_id)

        # G5: the branch's job is done -- its commits are on trunk. Archive
        # the engine's branch resource and drop its story links inside the
        # same transaction as the finalize.
        from .branches import cleanup_landed_branch
        cleaned = await cleanup_landed_branch(conn, node_id)

    # OUTSIDE the transaction, deliberately: a GitHub call inside it
    # would hold row locks for a network round-trip. The commits are on
    # trunk; deleting the ref is housekeeping.
    #
    # THIS NEVER ONCE WORKED, and said it had. It called `delete_ref`
    # with the token it was handed -- which comes from `detect`, minted
    # `_DETECT_PERMS` = contents:READ, because detection deliberately
    # cannot write. GitHub answers 403, `delete_ref` returns False, and
    # this ignored the return value and logged "deleted landed ref"
    # regardless. Two hundred and one branches on the test remote, every
    # merged one still there, and two hundred success lines in the log.
    #
    # `branch_cleanup` mints its OWN write-scoped token rather than
    # widening the detect token: detection staying read-only is a
    # property worth keeping, not an accident. It also carries the
    # guards this had none of -- engine-cut branches only, never a
    # trunk, never one another live node is still riding.
    from .branch_cleanup import delete_merged_branch

    # AND THE STORY RIDING IT MAY HAVE BEEN WAITING FOR THIS. A
    # coding story parks between push and merge so the person can
    # ask for one more thing while the tree is still there; the
    # merge is what answers that park.
    #
    # OUTSIDE THE TRANSACTION, beside the branch cleanup and for the
    # same reason -- it finishes a run, which is not work to do
    # while holding the finalize row locks. Best-effort: the
    # `sweep_landed` backstop finds anything this misses.
    try:
        from app.services.story_landing import on_node_landed
        await on_node_landed(get_pool(), node_id)
    except Exception:                                       # noqa: BLE001
        _log.exception("finalize: landing release failed for %s — the "
                       "sweep will retry", node_id)

    outcome = await delete_merged_branch(get_pool(), node_id)
    _log.info("finalize: branch cleanup for %s -> %s (archived %s "
              "resource row(s)); closed %s story thread(s) %s", node_id, outcome,
              (cleaned or {}).get("archived", 0), len(closed_threads),
              [t[:8] for t in closed_threads] or "")


async def _park(node_id: str, base_sha: str, reason: str) -> None:
    """Park a node CONFLICTED (with the trunk SHA the verdict was computed against)
    and dequeue. Re-queue happens after the assistant resolves + the engine
    re-checks (later steps)."""
    pool = get_pool()
    await pool.execute(
        "UPDATE git_nodes SET state = 'CONFLICTED', mergeable = 'no', "
        "  conflict_base_sha = $2, last_error = $3, updated_at = now() WHERE id = $1",
        node_id, base_sha or None, reason[:500],
    )
    await pool.execute(
        "UPDATE git_merge_queue SET status = 'parked' WHERE node_id = $1", node_id
    )


async def _protection_blocked(node_id: str, reason: str) -> None:
    # The node state carries the distinction (PROTECTION_BLOCKED); the queue row is
    # dequeued as 'parked' — git_merge_queue.status has no 'blocked' value, so DO
    # NOT write 'blocked' here (it would violate the CHECK).
    pool = get_pool()
    await pool.execute(
        "UPDATE git_nodes SET state = 'PROTECTION_BLOCKED', last_error = $2, "
        "  updated_at = now() WHERE id = $1",
        node_id, reason[:500],
    )
    await pool.execute(
        "UPDATE git_merge_queue SET status = 'parked' WHERE node_id = $1", node_id
    )
