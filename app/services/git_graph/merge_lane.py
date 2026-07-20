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
from typing import Optional

from app.database import get_pool

from . import github_ops as gh
from .locks import repo_locked
from .repo_identity import set_observed_trunk

_log = logging.getLogger(__name__)

_PR_PERMS = {"contents": "write", "metadata": "read", "pull_requests": "write"}


async def enqueue_ready_merges(conn) -> int:
    """Producer step (spec §0/§5/§7): enqueue a node when its work is done (state
    READY) and its merge story is still live. NO pre-PR approval gate — the human
    gate is the GitHub PR merge itself (the Git Engine "Approve" button is retired,
    §0). Transitions READY→QUEUED and inserts the durable queue row. DB-only,
    engine-only.

    Only READY nodes are enqueued; a CONFLICTED (parked) node is NOT auto-re-queued
    here. Idempotent: ON CONFLICT keeps the node's queue row (+ enqueue_seq, the FIFO key).

    (Retired the old `merge_approved_at IS NOT NULL` gate atomically with the
    approve-button retirement — §5 review: repoint enqueue here or the lease goes dark.)"""
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
        "RETURNING n.id AS node_id, n.git_repo_id, n.goal_id, "
        "          COALESCE(n.trunk_branch, r.trunk_branch) AS trunk_branch"
    )
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

    PRECONDITION (relied on by all three call sites — _cancel_node, reap, _finalize_merged):
    merge groups are SINGLE-NODE today (reconcile §6), so cancelling the whole group's
    engine code stories when ONE node settles is correct. If groups ever become multi-
    node, the per-node callers (_cancel_node/reap) must scope to the node's own stories,
    and _finalize_merged's call must stay gated on whole-group-landed (remaining == 0) —
    else a still-needed sibling node's diff story would be cancelled prematurely."""
    if not merge_group_id:
        return "UPDATE 0"
    return await executor.execute(
        "UPDATE stories SET state = 'cancelled', active_thread_id = NULL, updated_at = now() "
        "WHERE created_by = 'engine' AND story_kind = 'code' AND merge_group_id = $1 "
        "  AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')",
        merge_group_id,
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
                await conn.execute(
                    "UPDATE stories SET state = 'done', updated_at = now() "
                    "WHERE merge_group_id = $1 AND story_kind = 'merge' AND state <> 'done'",
                    gid,
                )
                # The whole group has LANDED → any in-flight engine integrate/fix (diff)
                # stories are now MOOT (the branch they were helping land is merged).
                # Cancel them so they don't orphan in_progress. The CANCEL/reject paths
                # already do this (detect._cancel_node / reap); the MERGE path must too,
                # else a human-merged PR strands its integrate story (observed live:
                # merge story 'done' but "Integrate dev into <branch>" stuck in_progress).
                await cancel_engine_stories_for_group(conn, gid)


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
