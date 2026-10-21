"""Persisting reconcile (plan §4) — Layer 1 topology (atomic) + Layer 2 stub.

`reconcile(goal_id)` derives the plan (pure, §4) and then, in ONE transaction,
materializes the git topology:

  * canonical `git_repos` rows (`ensure_git_repo`)
  * one `git_nodes` row per workstream (PLANNED; existing in-flight state never clobbered)
  * `git_node_stories` membership (handoff `seq`)
  * `git_merge_groups` + `git_merge_group_nodes` (single-node group per merge today)
  * the `story_kind='merge'` STORIES (created by the ENGINE — plan §6), one per
    workstream, depending on the workstream's last code story
  * the consumer→merge dependency EDGES (the lazy-cut gate, §4 rule 5): a
    workstream that depends on another gets its head story's `depends_on` pointed
    at the producer's merge story.

Idempotent: deterministic ids + ON CONFLICT DO NOTHING. Re-running never
duplicates and never resets a node's state.

Tolerant (per Step-2a review note): dangling refs / cycles leave some stories
unplaced; we persist the well-formed workstreams and surface the errors rather
than hard-failing the whole goal. The only hard abort is a cyclic
workstream-dependency graph (round-2 mandate: validate the augmented graph
before committing) — which cannot arise from an acyclic story DAG, so it is a
belt-and-suspenders guard.

Layer 2 (live-state resync — re-read GitHub ahead/behind/mergeable) is a STUB
until `github_ops` exists (build Step 4).

Branch creation, the `goal_resources` branch-bridge upsert, and base-SHA pinning
happen at branch-cut time (build Step 4), NOT here — nodes are created PLANNED.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from app.database import get_pool
from app.ledger.journal import append_event

from .plan import DerivedPlan, StoryInput, derive_plan, load_inputs
from .repo_identity import (ensure_git_repo, resolve_repo_trunk,
                            resolve_ws_trunk)

_log = logging.getLogger(__name__)

# Same system actor the story router uses for engine-authored ledger events.
from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID


def _h(prefix: str, ws_key: str, n: int) -> str:
    return prefix + hashlib.sha256(ws_key.encode("utf-8")).hexdigest()[:n]


def _node_id(ws_key: str) -> str:
    return _h("gn_", ws_key, 16)


def _group_id(ws_key: str) -> str:
    return _h("mg_", ws_key, 16)


def _merge_story_id(ws_key: str) -> str:
    return _h("merge-", ws_key, 12)


def _merge_thread_id(ws_key: str) -> str:
    return _h("thmerge-", ws_key, 12)


def _rows_affected(status: str) -> int:
    # asyncpg execute() returns e.g. "DELETE 2" / "UPDATE 1"
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


async def _gc_stale_planned(conn, goal_id: str, current_node_ids: list[str]) -> dict:
    """Tear down PLANNED nodes (+ their group membership) that the current plan no
    longer contains — e.g. a workstream whose stories were cancelled or recombined
    — and CANCEL their merge stories.

    Closes the 'stories change later' orphan gap: without this, a re-reconcile after
    a DAG edit leaves dead PLANNED nodes that `run_create_pass` would cut as PHANTOM
    branches, plus dead 'proposed' merge stories on the board. Re-reconcile is thus
    self-healing.

    PLANNED-ONLY by design: a node that is already cut / in-flight / merged is NEVER
    torn down here — abandoning a live branch (delete the ref, ABANDONED state) is a
    separate flow. Merge stories are CANCELLED (not deleted) for history + board
    consistency (the GO cancels stories too); a 'done' merge story is left untouched.
    """
    rows = await conn.fetch(
        "SELECT id FROM git_nodes "
        "WHERE goal_id = $1 AND state = 'PLANNED' AND id <> ALL($2::text[])",
        goal_id, current_node_ids,
    )
    orphan_ids = [r["id"] for r in rows]
    if not orphan_ids:
        return {"nodes_pruned": 0, "merge_stories_cancelled": 0}

    # Never auto-undo a human approval: approval lives in `merge_approved_at` (NOT in
    # state — git_graph_story_columns.sql), so a PLANNED node's merge CAN be approved.
    # Leave an approved orphan's merge story intact and surface it for review rather
    # than silently discarding a human decision.
    approved = await conn.fetch(
        "SELECT id FROM stories WHERE story_kind = 'merge' AND merge_approved_at IS NOT NULL "
        "  AND state NOT IN ('done', 'cancelled') AND merge_group_id IN ("
        "  SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = ANY($1::text[]))",
        orphan_ids,
    )
    if approved:
        _log.warning(
            "reconcile(%s): %d approved merge story(ies) belong to torn-down PLANNED "
            "workstreams; left intact for review (NOT cancelled): %s",
            goal_id, len(approved), [r["id"] for r in approved],
        )

    # Cancel only UN-approved, non-terminal orphan merge stories (idempotent: skips
    # already-cancelled).
    #
    # MULTI-NODE SAFE (multi-repo plan §11.6 / L3). Merge groups are no longer always
    # single-node: a multi-write story's group holds one node per repo. Cancelling a
    # group's merge story because ONE of its nodes was torn down would kill the merge
    # for the sibling repos that are still live. So cancel only when EVERY node in the
    # group is an orphan — i.e. the group has no surviving node. For single-node
    # groups this is identical to the previous behaviour.
    from app.services.story_state_ledger import record_states_where
    cancelled = await record_states_where(
        conn, "cancelled",
        where=("story_kind = 'merge' AND state NOT IN ('done', 'cancelled') "
               "AND merge_approved_at IS NULL AND merge_group_id IN ("
               "  SELECT gmn.merge_group_id FROM git_merge_group_nodes gmn "
               "   WHERE gmn.git_node_id = ANY($1::text[]) "
               "   GROUP BY gmn.merge_group_id "
               "  HAVING count(*) FILTER (WHERE NOT (gmn.git_node_id = "
               "ANY($1::text[]))) = 0 AND NOT EXISTS ("
               "    SELECT 1 FROM git_merge_group_nodes sib "
               "     WHERE sib.merge_group_id = gmn.merge_group_id "
               "       AND NOT (sib.git_node_id = ANY($1::text[]))))"),
        params=(orphan_ids,), action="merge_group_orphaned")
    # FK-safe order: children before parent. The PLANNED-only guard on the node DELETE
    # also protects git_merge_queue (node_id ON DELETE CASCADE) — a PLANNED node is
    # never enqueued, so the cascade can never drop a live queue row.
    await conn.execute(
        "DELETE FROM git_merge_group_nodes WHERE git_node_id = ANY($1::text[])", orphan_ids)
    await conn.execute(
        "DELETE FROM git_node_stories WHERE git_node_id = ANY($1::text[])", orphan_ids)
    pruned = await conn.execute(
        "DELETE FROM git_nodes WHERE id = ANY($1::text[]) AND state = 'PLANNED'", orphan_ids)
    _log.info("reconcile(%s): GC pruned %d stale PLANNED node(s), cancelled %d merge story(ies)",
              goal_id, _rows_affected(pruned), _rows_affected(cancelled))
    return {
        "nodes_pruned": _rows_affected(pruned),
        "merge_stories_cancelled": _rows_affected(cancelled),
    }


@dataclass
class ReconcileResult:
    goal_id: str
    nodes_created: int = 0
    nodes_existing: int = 0
    merge_stories_created: int = 0
    edges_wired: int = 0
    nodes_pruned: int = 0
    merge_stories_cancelled: int = 0
    unplaced: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    live_resync: str = ""
    plan: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "nodes_created": self.nodes_created,
            "nodes_existing": self.nodes_existing,
            "merge_stories_created": self.merge_stories_created,
            "edges_wired": self.edges_wired,
            "nodes_pruned": self.nodes_pruned,
            "merge_stories_cancelled": self.merge_stories_cancelled,
            "unplaced": self.unplaced,
            "errors": self.errors,
            "live_resync": self.live_resync,
            "plan": self.plan,
        }


def _ws_dag_acyclic(plan: DerivedPlan) -> bool:
    """Kahn over the workstream-dependency graph (consumer → producers). Belt-and-
    suspenders: ws_deps derives from the acyclic story DAG, so this never fails,
    but we honor the round-2 'validate the augmented graph before commit' mandate."""
    keys = [w.key for w in plan.workstreams]
    indeg = {k: 0 for k in keys}
    adj: dict[str, list[str]] = defaultdict(list)
    for consumer, producers in plan.ws_deps.items():
        for prod in producers:
            if prod in indeg and consumer in indeg:
                adj[prod].append(consumer)
                indeg[consumer] += 1
    queue = [k for k, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return seen == len(keys)


async def reconcile(goal_id: str) -> ReconcileResult:
    """Derive + persist the git topology for a goal. See module docstring."""
    pool = get_pool()
    async with pool.acquire() as conn:
        stories = await load_inputs(conn, goal_id)
        plan = derive_plan(goal_id, stories)

        if not _ws_dag_acyclic(plan):
            raise RuntimeError(
                f"reconcile({goal_id}) aborted: workstream dependency graph is cyclic"
            )

        by_id = {s.story_id: s for s in stories}
        async with conn.transaction():
            res = await _persist_topology(conn, goal_id, plan, by_id)

    res.unplaced = plan.unplaced
    res.errors = plan.errors
    res.plan = plan.to_dict()
    res.live_resync = "skipped: github_ops not built yet (build Step 4)"
    if plan.errors:
        _log.warning(
            "reconcile(%s): persisted %d workstreams with %d soft error(s): %s",
            goal_id, len(plan.workstreams), len(plan.errors), plan.errors,
        )
    return res


async def _persist_topology(
    conn, goal_id: str, plan: DerivedPlan, by_id: dict[str, StoryInput]
) -> ReconcileResult:
    res = ReconcileResult(goal_id=goal_id)
    ws_merge_story: dict[str, str] = {}        # ws key -> merge story id
    # Merge reasons are keyed by GROUP now (plan.merges is one entry per group). For
    # single-repo work group_key == ws.key, so this lookup is unchanged.
    reason_by_group = {m.workstream_key: m.reason for m in plan.merges}
    # Tracks which groups already had their merge story materialized this pass, so a
    # multi-write story's N workstreams create exactly ONE merge story between them.
    group_merge_story: dict[str, str] = {}
    keep_stories: list[str] = []               # parallel arrays: each placed story →
    keep_nodes: list[str] = []                 # its CURRENT node (for re-root prune)
    keep_repos: list[str] = []                 # and that node's repo (prune is per-repo)
    membership_nodes: list[str] = []           # node → its CURRENT merge group, so a
    membership_groups: list[str] = []          # superseded group membership can be pruned

    for ws in plan.workstreams:
        head = by_id[ws.story_ids[0]]
        # WHICH REPO THIS WORKSTREAM IS FOR. Must come from the WORKSTREAM, not from
        # the head story's scalar repo_* fields: a multi-write story has one workstream
        # per repo but only ONE scalar repo (its first write repo). Using the scalar
        # gave every one of its workstreams the same git_repo_id AND the same
        # branch_name, which violates UNIQUE (git_repo_id, branch_name) on git_nodes
        # and aborts the whole goal's reconcile transaction — and, absent the
        # constraint, would cut repo B's branch inside repo A.
        #
        # head.repos carries every resolved repo (repo_identity.resolve_story_repos);
        # match on the canonical id the workstream was keyed with. The scalar fields
        # remain the fallback for single-repo stories and for any StoryInput built
        # before `repos` was populated.
        ws_repo = next(
            (r for r in head.repos if r.get("git_repo_id") == ws.repo_id), None
        )
        repo_owner = (ws_repo or {}).get("repo_owner") or head.repo_owner
        repo_name = (ws_repo or {}).get("repo_name") or head.repo_name
        clone_url = (ws_repo or {}).get("clone_url") or head.clone_url
        # Trunk is a LANE fact first, a repo fact second: the trunk-lane
        # resource granted to this workstream's stories decides where it
        # lands (a repo may carry several trunk lanes in one goal --
        # backend and frontend branches of one app repo). With no lane
        # grant the goal's per-repo default holds, exactly as before.
        trunk_branch = getattr(ws, "lane", "") or await resolve_ws_trunk(
            conn, goal_id, repo_owner, repo_name, ws.story_ids)
        repo_id = await ensure_git_repo(
            conn,
            repo_owner,
            repo_name,
            clone_url=clone_url,
            trunk_branch=trunk_branch,
        )

        # 1. Node (one per workstream). Never clobber an in-flight node's state.
        #    trunk_branch is carried on the NODE (per-goal trunk, §0) so cut/compare/
        #    merge use THIS goal's trunk, not a repo-canonical last-write-wins value.
        node_id = _node_id(ws.key)
        created = await conn.fetchrow(
            "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state, trunk_branch) "
            "VALUES ($1, $2, $3, $4, 'PLANNED', $5) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            node_id, repo_id, goal_id, ws.branch_name, trunk_branch,
        )
        if created:
            res.nodes_created += 1
        else:
            res.nodes_existing += 1
        # Backfill trunk_branch for a node created before this column existed
        # (set-if-null; never re-point an established node's trunk).
        await conn.execute(
            "UPDATE git_nodes SET trunk_branch = $2 WHERE id = $1 AND trunk_branch IS NULL",
            node_id, trunk_branch,
        )

        # 2. Story membership (handoff order).
        for seq, sid in enumerate(ws.story_ids):
            await conn.execute(
                "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (git_node_id, story_id) DO UPDATE SET seq = EXCLUDED.seq",
                node_id, sid, seq,
            )
            keep_stories.append(sid)
            keep_nodes.append(node_id)
            keep_repos.append(repo_id)
        # Track this node's CURRENT group so stale memberships can be pruned below.
        membership_nodes.append(node_id)
        membership_groups.append(_group_id(ws.group_key))

        # 3. Merge group + group membership. A single-repo workstream is its own group
        #    (group_key == ws.key, so the id is unchanged). A multi-write story's N
        #    workstreams share one group_key, so all N nodes land in ONE group and the
        #    existing whole-group gate in _finalize_merged holds the merge story open
        #    until every repo's node is MERGED.
        group_id = _group_id(ws.group_key)
        await conn.execute(
            "INSERT INTO git_merge_groups (id, goal_id) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            group_id, goal_id,
        )
        # seq = intra-group landing order. Within one story's repos there is no
        # declared ordering to honour, so it is the deterministic sorted-repo order
        # derive_plan produced; the merge lane treats it as advisory ordering, not a
        # hard gate. Single-node groups keep seq 0 exactly as before.
        node_seq = ws.group_seq
        await conn.execute(
            "INSERT INTO git_merge_group_nodes (merge_group_id, git_node_id, seq) "
            "VALUES ($1, $2, $3) ON CONFLICT (merge_group_id, git_node_id) "
            "DO UPDATE SET seq = EXCLUDED.seq",
            group_id, node_id, node_seq,
        )

        # 4. Materialize the merge STORY (engine-authored; plan §6) — ONCE per group.
        #    The second and later workstreams of a multi-write story reuse the merge
        #    story the first one created; creating one per workstream would give the
        #    story N merge approvals instead of the single one the design requires.
        # D1/R2-F3 — AN ERRAND GETS NO MERGE STORY.
        #
        # An errand is allocated what a story is allocated, its own
        # branch included, and its work never LANDS. A merge story is the
        # unit of landing, so one for an errand is a control nobody
        # should ever press: `enqueue_ready_merges` holds the node
        # whatever happens to the story, so approving it lands nothing
        # and says nothing about why.
        #
        # Observed: errand b9f0ceb8 finished, its node parked correctly
        # at READY, and `merge-8361abfd4f8a` sat on the board in
        # `proposed` waiting for a human to approve a landing the
        # platform guarantees will not happen. GO's own prompt says an
        # errand has "no merge story and no landing wake".
        #
        # HERE AND NOT IN DERIVATION. The branch is still cut — that is
        # D1 — and the plan layer stays ignorant of `work_kind`, which
        # keeps one topology rather than two.
        errand_only = await conn.fetchval(
            """
            SELECT bool_and(work_kind = 'errand')
              FROM stories WHERE id = ANY($1::text[])
            """, list(ws.story_ids))
        if errand_only:
            _log.info("reconcile: workstream %s is an errand — branch "
                        "derived, no merge story (its work never lands)",
                        ws.key)
            continue

        merge_id = _merge_story_id(ws.group_key)
        ws_merge_story[ws.key] = merge_id
        if ws.group_key in group_merge_story:
            continue
        group_merge_story[ws.group_key] = merge_id
        assignee = ws.owners[-1] if ws.owners else ""   # last owner; goal-admin fallback at approval (L10)
        reason = reason_by_group.get(ws.group_key, "ship")
        # Name the repo this workstream actually targets, not the head's scalar — on a
        # multi-write story the scalar is always the FIRST repo, so every merge story
        # would claim to be for repo A.
        title = f"Merge {repo_name}: {ws.branch_name}"
        desc = (
            f"Land workstream {ws.key} ({reason}) to trunk. "
            "Collaborative, human-approved merge: sync trunk, review the diff, run "
            "CI on the merged result, resolve conflicts; the engine performs the git "
            "land on approval."
        )
        thread_id = _merge_thread_id(ws.group_key)
        # Upsert: refresh depends_on/title/assignee on RE-reconcile so the merge
        # story tracks the CURRENT workstream (its `depends_on` = the last story in
        # the handoff chain). Without this, adding/removing a member story leaves the
        # merge story pinned to its original last-story — a stale, misleading
        # dependency on the board. Guarded to `proposed` + unapproved so we never
        # clobber a merge that's already been engaged (approved/landing/done). The
        # node-READY gate (advance_node_lifecycle, membership-driven) is the real
        # merge gate; this keeps the merge story's *displayed* dependency honest.
        # (xmax = 0) distinguishes a true INSERT from the conflict-UPDATE so the
        # thread + story.created event fire exactly once, at creation.
        made = await conn.fetchrow(
            "INSERT INTO stories (id, goal_id, title, description, assigned_to, "
            "  state, depends_on, thread_id, created_by, story_kind, merge_group_id) "
            "VALUES ($1, $2, $3, $4, $5, 'proposed', $6, $7, 'engine', 'merge', $8) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  depends_on = EXCLUDED.depends_on, "
            "  title = EXCLUDED.title, "
            "  assigned_to = EXCLUDED.assigned_to, "
            "  description = EXCLUDED.description "
            "  WHERE stories.state = 'proposed' AND stories.merge_approved_at IS NULL "
            "RETURNING id, (xmax = 0) AS inserted",
            merge_id, goal_id, title, desc, assignee,
            [ws.story_ids[-1]], thread_id, group_id,
        )
        if made and made["inserted"]:
            res.merge_stories_created += 1
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, title) "
                "VALUES ($1, $2, 'engine', $3) ON CONFLICT (id) DO NOTHING",
                thread_id, goal_id, f"Merge: {ws.branch_name[:70]}",
            )
            await append_event(
                conn,
                actor_id=_SYSTEM_ACTOR_ID, actor_type="system",
                event_type="story.created",
                payload={
                    "story_id": merge_id,
                    "title": title,
                    "story_kind": "merge",
                    "thread_id": thread_id,
                    "merge_group_id": group_id,
                    "assigned_user_id": assignee or "",
                },
                context_goal_id=goal_id,
            )

    # 4b. Prune stale membership when a story re-roots to a different node across
    # reconciles (DAG changed → different head → different node_id). Only re-points
    # CURRENTLY-PLACED stories (story_id in the keep-set but under a different node),
    # so a story that is transiently unplaced (e.g. a cycle this run) is never touched
    # and removed-story orphan rows are left for a later GC.
    #
    # PARTITIONED BY REPO (multi-repo plan §11.6 / R2). A multi-write story is placed
    # in one node PER REPO, so it legitimately appears in `keep_stories` more than
    # once with different node ids. The original unqualified form
    # (`story_id = cur.sid AND git_node_id <> cur.nid`) matched each of the story's
    # memberships against the OTHER row's node id and deleted BOTH — silently
    # unplacing the story from every repo. Restricting the delete to nodes in the SAME
    # repo keeps the re-root dedup this exists for while leaving sibling-repo
    # memberships alone. Single-repo stories appear once and behave exactly as before.
    if keep_stories:
        # KEEP-SET SEMANTICS, not pairwise. The per-repo partition above
        # was itself a fix -- for multi-REPO stories, whose two nodes sit
        # in different repos and so never matched each other. LANES broke
        # it again: a story writing cal_backend and cal_frontend has two
        # nodes in the SAME repo, so each keep entry deleted the other's
        # membership and the story ended up placed nowhere -- two nodes
        # with no riders, never cut, silently stuck at PLANNED.
        #
        # A membership survives when its exact (story, node) pair is one
        # this derivation produced. Re-rooting still prunes (the old pair
        # is absent from the keep set), multi-repo still works, and any
        # number of lanes per repo works for the same reason.
        await conn.execute(
            "DELETE FROM git_node_stories gns "
            "USING git_nodes n "
            "WHERE n.id = gns.git_node_id "
            "  AND gns.story_id = ANY($1::text[]) "
            "  AND n.git_repo_id = ANY($3::text[]) "
            "  AND NOT EXISTS ("
            "        SELECT 1 FROM unnest($1::text[], $2::text[]) AS k(sid, nid) "
            "         WHERE k.sid = gns.story_id AND k.nid = gns.git_node_id)",
            keep_stories, keep_nodes, keep_repos,
        )

    # 4c. Prune SUPERSEDED merge-group membership.
    #
    # A node's group can change without the node itself changing: granting a second
    # repo to an in-flight story flips its group_key from '<repo_id>:<head>' to
    # 'story:<head>', so reconcile inserts membership in the NEW group while the OLD
    # row survives. The node then sits in two groups and the old group keeps a live
    # merge story — a second, stale human-approval surface for the same work, and
    # grind's group lookup could bind a diff/fix story to whichever it happened to see.
    #
    # Reconcile is the ONLY writer of git_merge_group_nodes (verified), so a node
    # legitimately belongs to exactly one group: the one its current workstream names.
    # Anything else is stale by construction.
    if membership_nodes:
        await conn.execute(
            "DELETE FROM git_merge_group_nodes gmn "
            "USING unnest($1::text[], $2::text[]) AS cur(nid, gid) "
            "WHERE gmn.git_node_id = cur.nid AND gmn.merge_group_id <> cur.gid",
            membership_nodes, membership_groups,
        )
        # Cancel the merge story of any group that just lost its last node, then drop
        # the empty group. Guarded to an untouched 'proposed' merge story so a group
        # whose merge is already engaged (approved / landing / done) is never disturbed.
        emptied = await conn.fetch(
            "SELECT g.id FROM git_merge_groups g "
            " WHERE g.goal_id = $1 "
            "   AND NOT EXISTS (SELECT 1 FROM git_merge_group_nodes gmn "
            "                    WHERE gmn.merge_group_id = g.id)",
            goal_id,
        )
        for row in emptied:
            await conn.execute(
                "UPDATE stories SET state = 'cancelled', updated_at = now() "
                "WHERE story_kind = 'merge' AND merge_group_id = $1 "
                "  AND state = 'proposed' AND merge_approved_at IS NULL",
                row["id"],
            )
            await conn.execute("DELETE FROM git_merge_groups WHERE id = $1", row["id"])
        if emptied:
            _log.info(
                "reconcile(%s): pruned %d superseded merge group(s) after a node's "
                "group changed", goal_id, len(emptied),
            )

    # 5. Wire consumer→merge dependency edges (lazy-cut gate, §4 rule 5).
    ws_by_key = {w.key: w for w in plan.workstreams}
    for consumer_key, producer_keys in plan.ws_deps.items():
        consumer = ws_by_key.get(consumer_key)
        if consumer is None:
            continue
        head_sid = consumer.story_ids[0]
        for prod_key in producer_keys:
            merge_id = ws_merge_story.get(prod_key)
            if not merge_id:
                continue
            wired = await conn.fetchrow(
                "UPDATE stories SET depends_on = array_append(depends_on, $2) "
                "WHERE id = $1 AND NOT ($2 = ANY(depends_on)) RETURNING id",
                head_sid, merge_id,
            )
            if wired:
                res.edges_wired += 1

    # 5b. Un-wire STALE merge deps — the REMOVE half of the add above (§4 rule 5).
    # array_append never removes, so a consumer that STOPS depending on a producer
    # (the DAG was edited, or the consumer re-rooted) keeps the dead merge_id pinned in
    # depends_on forever → the consumer is blocked permanently with no self-heal. For
    # each head, strip any MERGE-story dep that isn't one of its CURRENT producers.
    # Scoped to merge-story ids so human/GO code-story deps are never touched; the
    # cancel path (GC → merge story 'cancelled') is already self-healing, this covers
    # the dependency-dropped / re-rooted cases the cancel path misses.
    valid_merge_deps: dict[str, set] = {}
    for consumer_key, producer_keys in plan.ws_deps.items():
        consumer = ws_by_key.get(consumer_key)
        if consumer is None:
            continue
        valid_merge_deps.setdefault(consumer.story_ids[0], set()).update(
            ws_merge_story[p] for p in producer_keys if p in ws_merge_story
        )
    merge_ids = [
        r["id"] for r in await conn.fetch(
            "SELECT id FROM stories WHERE goal_id = $1 AND story_kind = 'merge'", goal_id
        )
    ]
    if merge_ids:
        merge_set = set(merge_ids)
        holders = await conn.fetch(
            "SELECT id, depends_on FROM stories "
            "WHERE goal_id = $1 AND depends_on && $2::text[]",
            goal_id, merge_ids,
        )
        for h in holders:
            valid = valid_merge_deps.get(h["id"], set())
            stale = [d for d in (h["depends_on"] or []) if d in merge_set and d not in valid]
            if stale:
                await conn.execute(
                    "UPDATE stories SET depends_on = COALESCE("
                    "  (SELECT array_agg(e) FROM unnest(depends_on) e "
                    "   WHERE NOT (e = ANY($2::text[]))), '{}'), "
                    "  updated_at = now() WHERE id = $1",
                    h["id"], stale,
                )
                _log.info(
                    "reconcile(%s): un-wired %d stale merge dep(s) from story %s",
                    goal_id, len(stale), h["id"],
                )

    # 6. GC stale PLANNED nodes + merge stories whose workstream is no longer in
    #    the plan (cancelled / recombined stories). Makes re-reconcile self-healing
    #    and stops the engine cutting phantom branches. PLANNED-only (§ helper).
    current_node_ids = [_node_id(w.key) for w in plan.workstreams]
    gc = await _gc_stale_planned(conn, goal_id, current_node_ids)
    res.nodes_pruned = gc["nodes_pruned"]
    res.merge_stories_cancelled = gc["merge_stories_cancelled"]

    return res
