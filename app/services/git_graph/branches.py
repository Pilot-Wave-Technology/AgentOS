"""Node branch creation + read-only status (plan §5.1 create path, Phase 2).

The lazy-cut state machine for a workstream node:

    PLANNED → BRANCHING (pin base_sha = live trunk, durable pre-API) → BRANCH_CREATED

A node is cut only when it is "cut-ready": all the merge stories its head story
depends on are landed (state 'done'). Root nodes (no merge-story deps) cut
immediately; dependent nodes wait for their producer's merge (Step 7) — that's
the lazy-cut gate (plan §4 rule 2 / §4 rule 5).

ALL state transitions are guarded CAS (UPDATE ... WHERE state=$expected), and
branch creation is idempotent + crash-recoverable: a node left in BRANCHING is
resumed by re-asserting the ref (create_ref treats an existing ref as success).

This module does NOT sync/merge/restack — those touch live branches and wait for
the Step 5 sync contract. It also does NOT yet write the goal_resources
branch-bridge row (that pairs with provisioning in Step 5).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from app.database import get_pool

from . import github_ops as gh
from .dispatch import notify_story_branch_ready
from .node_errors import clear_node_error, record_node_github_failure
from .repo_identity import set_observed_trunk

_log = logging.getLogger(__name__)


async def _repo_info(conn, git_repo_id: str) -> tuple[str, str, str, str]:
    row = await conn.fetchrow(
        "SELECT owner, name, trunk_branch, clone_url FROM git_repos WHERE id = $1",
        git_repo_id,
    )
    if row is None:
        raise RuntimeError(f"git_repos row {git_repo_id} missing")
    return row["owner"], row["name"], row["trunk_branch"] or "main", row["clone_url"] or ""


async def ensure_branch_resource(
    conn, node: dict, owner: str, name: str, clone_url: str, base_branch: str
) -> str:
    """Branch-name bridge (plan §5.5.8): register the engine-cut branch as a
    `goal_resources` kind='branch' row + link the node's member stories to it via
    `story_resources`, so the EXISTING provisioning resolver (_resolve_repo_for_story)
    finds it.

    Idempotent via check-then-insert (the single-leader engine is the only writer,
    so no unique-index is needed — and a hard unique index would risk failing on
    pre-existing duplicate branch rows from the legacy create_branch path).
    Does NOT call internal_create_branch (which re-pulls the session-dependent
    refresh_story_resources — the C2 path §5.5.7 avoids)."""
    goal_id = node["goal_id"]
    branch = node["branch_name"]
    repo_id = node["git_repo_id"]

    existing = await conn.fetchrow(
        "SELECT id FROM goal_resources "
        "WHERE goal_id = $1 AND kind = 'branch' AND repo_name = $2 AND branch_name = $3 "
        "  AND status = 'active' LIMIT 1",
        goal_id, name, branch,
    )
    if existing:
        resource_id = existing["id"]
        await conn.execute(
            "UPDATE goal_resources SET git_repo_id = $2 WHERE id = $1 AND git_repo_id IS NULL",
            resource_id, repo_id,
        )
    else:
        parent = await conn.fetchrow(
            "SELECT id FROM goal_resources WHERE goal_id = $1 AND kind = 'repo' "
            "  AND repo_name = $2 LIMIT 1",
            goal_id, name,
        )
        resource_id = "gb_" + uuid.uuid4().hex[:12]
        await conn.execute(
            "INSERT INTO goal_resources (id, goal_id, kind, role, name, uri, created_by, "
            "  repo_owner, repo_name, branch_name, base_branch, clone_url, parent_id, "
            "  git_repo_id, status, summary_stale, default_writable) "
            # role='feature': this is the story's WORKING branch (vs role='trunk'), so the
            # resolvers' `ORDER BY (role='feature') DESC` reliably prefers it.
            #
            # F2 — default_writable TRUE, and it was the column default
            # (false) before. This row is granted to its member stories
            # as `produces` four lines below -- a WRITE relation -- so a
            # ceiling of "may never be written" contradicted the grant
            # being made in the same function.
            #
            # What that cost: `list_resources` reports `writable` from the
            # mount and `can_write` from this column, and the assistant is
            # told in as many words that "`can_write` false means
            # read-only by policy". So the lane came back as writable AND
            # never-writable at once, and the assistant -- correctly --
            # refused to guess which branch to work on and asked a human.
            # A round trip per story, presenting as hesitancy rather than
            # as bad data.
            #
            # A feature lane exists SO THAT the work can be written to it.
            # Writing is not an exception to grant, it is the purpose.
            "VALUES ($1, $2, 'branch', 'feature', $3, $4, 'engine', $5, $6, $7, $8, $9, $10, $11, "
            "  'active', true, true)",
            resource_id, goal_id, f"{name}:{branch}",
            f"https://github.com/{owner}/{name}/tree/{branch}",
            owner, name, branch, base_branch, clone_url,
            parent["id"] if parent else None, repo_id,
        )

    # Link every member story so provisioning for ANY of them resolves this branch.
    members = await conn.fetch(
        "SELECT story_id FROM git_node_stories WHERE git_node_id = $1", node["id"]
    )
    for m in members:
        await conn.execute(
            "INSERT INTO story_resources (story_id, resource_id, relation) "
            "VALUES ($1, $2, 'produces') ON CONFLICT DO NOTHING",
            m["story_id"], resource_id,
        )
    return resource_id


async def _is_cut_ready(conn, node_id: str) -> bool:
    """A PLANNED node is ready to cut when no merge-story dependency of its HEAD
    story is still un-landed. (A merge story flips to 'done' only when its whole
    group is MERGED — Step 7 — so dependent nodes wait for their producer.)"""
    # Never cut a stale workstream: a node whose every member story is terminal
    # (cancelled/rejected) — or that has no members — represents work that no
    # longer exists. Defense-in-depth alongside the reconcile GC so a phantom
    # branch is never cut even in the window before a re-reconcile prunes it.
    has_live = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM git_node_stories gns JOIN stories s ON s.id = gns.story_id "
        "  WHERE gns.git_node_id = $1 AND s.state NOT IN ('cancelled', 'rejected'))",
        node_id,
    )
    if not has_live:
        return False
    # Cut LAZILY — only once work has actually STARTED on the workstream. A node whose
    # members are all still 'proposed'/'accepted' is the GO drafting/iterating the plan;
    # cutting it would create (and, if a draft is then cancelled → settled → READY,
    # even no-op-MERGE) branches for stories nobody has begun. reconcile persists these
    # as PLANNED for the plan preview; the branch is cut when a member reaches
    # in_progress (at import/provision). 'done'/'reviewed' members count as started too
    # (a chain whose head already finished). 'blocked' is reached only from in_progress.
    has_started = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM git_node_stories gns JOIN stories s ON s.id = gns.story_id "
        "  WHERE gns.git_node_id = $1 "
        "    AND s.state IN ('in_progress', 'blocked', 'awaiting_merge', "
        "                 'done', 'reviewed'))",
        node_id,
    )
    if not has_started:
        return False
    head = await conn.fetchrow(
        "SELECT story_id FROM git_node_stories WHERE git_node_id = $1 "
        "ORDER BY seq ASC LIMIT 1",
        node_id,
    )
    if head is None:
        # Defensive only: unreachable now that the has_live guard above returns
        # False for a node with zero members. Do NOT "simplify" this back into a
        # cut-ready=True for member-less nodes — that reopens phantom cuts.
        return True
    deps = await conn.fetchval(
        "SELECT depends_on FROM stories WHERE id = $1", head["story_id"]
    )
    if not deps:
        return True
    # A merge dependency is satisfied (doesn't block the cut) when it merged
    # ('done'/'reviewed') OR its producer workstream was torn down ('cancelled' = GC
    # removed it → obsolete dep). It STILL BLOCKS when 'rejected' (an explicit human
    # merge-cancel — §12 — where dependents must stay blocked for re-plan) or when
    # still in-flight. Same rule as story_transitions._DEP_SATISFIED_SQL (cut/start/
    # import gates consistent — spec §0/§12).
    unmet = await conn.fetchval(
        "SELECT count(*) FROM stories "
        "WHERE id = ANY($1) AND story_kind = 'merge' "
        "  AND state NOT IN ('done', 'reviewed', 'cancelled')",
        list(deps),
    )
    return (unmet or 0) == 0


async def try_create_branch(conn, node: dict) -> str:
    """Drive PLANNED/BRANCHING → BRANCH_CREATED for one node. Idempotent + crash-safe.

    Returns 'branch_created' on success, 'skipped' if the node's state changed
    under us (lost the CAS)."""
    owner, name, repo_trunk, clone_url = await _repo_info(conn, node["git_repo_id"])
    # Per-goal trunk (§0): the NODE's trunk_branch is authoritative; fall back to the
    # repo default only for a pre-migration node that hasn't been backfilled yet.
    trunk_branch = node.get("trunk_branch") or repo_trunk
    branch = node["branch_name"]
    token = await gh.mint_repo_token(name)  # default perms: contents:write covers create-ref

    if node["state"] == "PLANNED":
        # Durable pre-API: pin base_sha from LIVE trunk, then CAS into BRANCHING
        # BEFORE the create-ref call (so a crash mid-create is recoverable).
        base_sha = await gh.get_trunk_sha(token, owner, name, trunk_branch)
        claimed = await conn.fetchrow(
            "UPDATE git_nodes SET state = 'BRANCHING', base_sha = $2, updated_at = now() "
            "WHERE id = $1 AND state = 'PLANNED' RETURNING id",
            node["id"], base_sha,
        )
        if claimed is None:
            return "skipped"  # someone else advanced it
    else:  # resume a BRANCHING node after a crash
        base_sha = node.get("base_sha")
        if not base_sha:  # defensive: re-pin if the pre-API write was lost
            base_sha = await gh.get_trunk_sha(token, owner, name, trunk_branch)
            await conn.execute(
                "UPDATE git_nodes SET base_sha = $2 WHERE id = $1 AND state = 'BRANCHING'",
                node["id"], base_sha,
            )

    # Idempotent create: 'created' or 'exists' both mean the ref is present at the
    # recorded base (we never re-point an existing ref to a moved tip — plan §5.1).
    await gh.create_ref(token, owner, name, branch, base_sha)

    await conn.execute(
        "UPDATE git_nodes SET state = 'BRANCH_CREATED', updated_at = now() "
        "WHERE id = $1 AND state = 'BRANCHING'",
        node["id"],
    )

    # Branch-name bridge (§5.5.8): register the branch as a goal_resources row +
    # link member stories, so the existing provisioning resolver finds it.
    try:
        await ensure_branch_resource(conn, node, owner, name, clone_url, trunk_branch)
        # Component C: the branch is now cut + linked — signal the web tier to
        # re-provision any member story that was lazy-waiting for this cut (a story
        # whose SHCA kickoff returned {waiting:True}). Best-effort + a poll backstop
        # in the web-tier listener covers a missed NOTIFY.
        members = await conn.fetch(
            "SELECT story_id FROM git_node_stories WHERE git_node_id = $1", node["id"]
        )
        for m in members:
            await notify_story_branch_ready(node["goal_id"], m["story_id"])
    except Exception:
        _log.exception(
            "git-engine: branch-bridge upsert / branch-ready notify failed for node %s",
            node["id"],
        )

    return "branch_created"


async def refresh_node_status(conn, node: dict) -> dict:
    """Read-only ahead/behind of a node's branch vs live trunk (plan §11 status).

    Also refreshes the observed trunk_sha cache (detection only). Does NOT mutate
    the branch or the node's lifecycle state."""
    owner, name, repo_trunk, _ = await _repo_info(conn, node["git_repo_id"])
    trunk_branch = node.get("trunk_branch") or repo_trunk
    token = await gh.mint_repo_token(name)
    trunk_sha = await gh.get_trunk_sha(token, owner, name, trunk_branch)
    # Per-(repo,trunk) observed cache (§0) + the legacy git_repos.trunk_sha (display).
    await set_observed_trunk(conn, node["git_repo_id"], trunk_branch, trunk_sha)
    await conn.execute(
        "UPDATE git_repos SET trunk_sha = $2, trunk_sha_seen_at = now() WHERE id = $1",
        node["git_repo_id"], trunk_sha,
    )
    cmp = await gh.compare(token, owner, name, trunk_sha, node["branch_name"])
    await conn.execute(
        "UPDATE git_nodes SET live_resynced_at = now() WHERE id = $1", node["id"]
    )
    return {
        "node_id": node["id"],
        "branch": node["branch_name"],
        "trunk_sha": trunk_sha,
        "ahead_by": cmp["ahead_by"],
        "behind_by": cmp["behind_by"],
        "status": cmp["status"],
    }


async def run_create_pass(goal_id: Optional[str] = None) -> dict:
    """Engine tick (Phase 2): cut branches for all cut-ready PLANNED nodes and
    resume any BRANCHING nodes. Single-leader, sequential — no per-repo lock needed
    yet (lane work that needs it lands in Step 6). Read-only status is NOT auto-run
    here (rate-limit discipline; it's a callable for the status endpoint)."""
    pool = get_pool()
    created = resumed = skipped = 0
    async with pool.acquire() as conn:
        q = (
            "SELECT id, git_repo_id, goal_id, branch_name, base_sha, state, trunk_branch "
            "FROM git_nodes WHERE state IN ('PLANNED', 'BRANCHING')"
        )
        rows = await conn.fetch(q + (" AND goal_id = $1" if goal_id else ""),
                                *( (goal_id,) if goal_id else () ))
        for r in rows:
            node = dict(r)
            was_branching = node["state"] == "BRANCHING"
            if node["state"] == "PLANNED" and not await _is_cut_ready(conn, node["id"]):
                skipped += 1
                continue
            try:
                result = await try_create_branch(conn, node)
            except Exception as exc:
                # Surface on the node (UI error column) instead of log-only: a 401/403/404
                # (lost contents:write, missing trunk ref) escalates + holds; a transient
                # error is recorded + retried next tick. Previously a failed cut left the
                # node in PLANNED/BRANCHING with an empty error — looked clean, stuck looping.
                res = await record_node_github_failure(node["id"], exc, context="branch-cut")
                _log.exception("git-engine: branch create failed for node %s (%s) — %s",
                               node["id"], node["branch_name"], res)
                continue
            if result == "branch_created":
                await clear_node_error(conn, node["id"])  # recovered → clear stale error
                if was_branching:
                    resumed += 1
                else:
                    created += 1
            else:
                skipped += 1
    return {"created": created, "resumed": resumed, "skipped": skipped}


def _rows_affected(status: str) -> int:
    # asyncpg execute() returns e.g. "UPDATE 3"
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


async def advance_node_lifecycle(conn) -> dict:
    """Advance node states off the code-story lifecycle (engine-only, DB-only).

    BRANCH_CREATED → IN_PROGRESS  when any member (code) story is in_progress —
        this is what makes the `synced-to` cooperative-sync CAS (§5.5.2) live.
    BRANCH_CREATED|IN_PROGRESS → READY  when every member story is SETTLED
        (done/reviewed/cancelled/rejected/awaiting_merge)

        `awaiting_merge` IS SETTLED FOR THIS PURPOSE, and leaving it out
        deadlocked the lane: a coding story now waits at `awaiting_merge`
        for its work to LAND, and the node has to reach READY for a PR to
        be opened at all. Story waits for the merge, node waits for the
        story to be done, neither moves, no PR. Observed on dev with
        caf071a4. "Settled" here means the branch's work is complete --
        which is exactly what `awaiting_merge` says. — the branch's work is complete → its merge
        story can be worked / queued. ('reviewed' = a done story the council reviewed,
        i.e. done-and-then-some — MUST count as settled or a reviewed story sticks the
        node at IN_PROGRESS forever. "settled" rather than strictly "done" also means a
        cancelled member can't permanently stick the node; a degenerate all-cancelled
        node reaching READY with no landed work is left for Step-7/GC abandon
        handling.)

    Member stories are the node's code stories (git_node_stories); the merge story
    is NOT a member, so it never gates these transitions. State machine guards
    (WHERE state IN ...) ensure nodes past READY (QUEUED/MERGING/…) are untouched.
    Run sequentially in the single leader — the two UPDATEs compose: a node whose
    work all finished this tick goes BRANCH_CREATED→READY directly."""
    to_in_progress = await conn.execute(
        "UPDATE git_nodes SET state = 'IN_PROGRESS', updated_at = now() "
        "WHERE state = 'BRANCH_CREATED' AND EXISTS ("
        "  SELECT 1 FROM git_node_stories gns JOIN stories s ON s.id = gns.story_id "
        "  WHERE gns.git_node_id = git_nodes.id AND s.state = 'in_progress')"
    )
    to_ready = await conn.execute(
        "UPDATE git_nodes SET state = 'READY', updated_at = now() "
        "WHERE state IN ('BRANCH_CREATED', 'IN_PROGRESS') "
        "  AND EXISTS (SELECT 1 FROM git_node_stories WHERE git_node_id = git_nodes.id) "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM git_node_stories gns JOIN stories s ON s.id = gns.story_id "
        "    WHERE gns.git_node_id = git_nodes.id "
        "      AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected', 'awaiting_merge'))"
    )
    return {
        "to_in_progress": _rows_affected(to_in_progress),
        "to_ready": _rows_affected(to_ready),
    }


async def demote_stale_ready(conn) -> int:
    """READY → IN_PROGRESS when a member story is NO LONGER settled — the reverse of
    the READY promotion above.

    Covers a workstream that gained a new (proposed/in_progress) member AFTER its node
    first reached READY: without this the node would merge on the stale membership it
    held when it hit READY, dropping the late-arriving story (exactly the bug where a
    chain member created between reconciles was merged past). The engine runs a
    merge-boundary reconcile just before this, so a freshly-added member is in
    git_node_stories by the time we re-check.

    Pre-merge ONLY (state='READY'): a node already QUEUED/MERGING has an open PR and is
    out of scope (handling that would mean holding/cancelling an open PR — a separate
    policy). Same 'settled' set as the promotion so the two compose without flapping."""
    res = await conn.execute(
        "UPDATE git_nodes SET state = 'IN_PROGRESS', updated_at = now() "
        "WHERE state = 'READY' AND EXISTS ("
        "  SELECT 1 FROM git_node_stories gns JOIN stories s ON s.id = gns.story_id "
        "  WHERE gns.git_node_id = git_nodes.id "
        "    AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected', 'awaiting_merge'))"
    )
    return _rows_affected(res)

async def cleanup_landed_branch(conn, node_id: str) -> dict:
    """G5, the database half of delete-on-merge: archive the engine's
    branch resource and drop its story links, so landed branches stop
    accumulating in the catalogue the way refs used to on GitHub.

    ONLY engine-created rows. A human-registered branch (a trunk
    designation, a long-lived integration branch) is the human's to
    retire. Returns what it did so the caller can log honestly.
    """
    node = await conn.fetchrow(
        "SELECT branch_name, git_repo_id, goal_id FROM git_nodes "
        "WHERE id = $1", node_id)
    if node is None:
        return {"archived": 0, "unlinked": 0}
    rows = await conn.fetch(
        """
        UPDATE goal_resources SET status = 'archived'
         WHERE goal_id = $1 AND kind = 'branch'
           AND branch_name = $2 AND git_repo_id = $3
           AND created_by = 'engine' AND status <> 'archived'
        RETURNING id
        """,
        node["goal_id"], node["branch_name"], node["git_repo_id"])
    unlinked = 0
    for r in rows:
        result = await conn.execute(
            "DELETE FROM story_resources WHERE resource_id = $1", r["id"])
        unlinked += int(result.split()[-1])
    return {"archived": len(rows), "unlinked": unlinked,
            "branch": node["branch_name"]}
