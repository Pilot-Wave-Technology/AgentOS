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
from .repo_identity import ensure_git_repo, resolve_repo_trunk

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
    # already-cancelled). Single-node merge groups today, so an orphan's group never
    # contains a live sibling node.
    cancelled = await conn.execute(
        "UPDATE stories SET state = 'cancelled', updated_at = now() "
        "WHERE story_kind = 'merge' AND state NOT IN ('done', 'cancelled') "
        "  AND merge_approved_at IS NULL AND merge_group_id IN ("
        "  SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = ANY($1::text[]))",
        orphan_ids,
    )
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
    reason_by_ws = {m.workstream_key: m.reason for m in plan.merges}
    keep_stories: list[str] = []               # parallel arrays: each placed story →
    keep_nodes: list[str] = []                 # its CURRENT node (for re-root prune)

    for ws in plan.workstreams:
        head = by_id[ws.story_ids[0]]
        # Trunk is a repo-level fact: role='trunk' resource → repo default_branch →
        # 'main'. NOT head.default_branch (a feature branch resource's value is
        # incidental and would let 'main' clobber a configured trunk).
        trunk_branch = await resolve_repo_trunk(conn, goal_id, head.repo_owner, head.repo_name)
        repo_id = await ensure_git_repo(
            conn,
            head.repo_owner,
            head.repo_name,
            clone_url=head.clone_url,
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

        # 3. Merge group + group membership (single-node group today).
        group_id = _group_id(ws.key)
        await conn.execute(
            "INSERT INTO git_merge_groups (id, goal_id) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            group_id, goal_id,
        )
        await conn.execute(
            "INSERT INTO git_merge_group_nodes (merge_group_id, git_node_id, seq) "
            "VALUES ($1, $2, 0) ON CONFLICT (merge_group_id, git_node_id) DO NOTHING",
            group_id, node_id,
        )

        # 4. Materialize the merge STORY (engine-authored; plan §6).
        merge_id = _merge_story_id(ws.key)
        ws_merge_story[ws.key] = merge_id
        assignee = ws.owners[-1] if ws.owners else ""   # last owner; goal-admin fallback at approval (L10)
        reason = reason_by_ws.get(ws.key, "ship")
        title = f"Merge {head.repo_name}: {ws.branch_name}"
        desc = (
            f"Land workstream {ws.key} ({reason}) to trunk. "
            "Collaborative, human-approved merge: sync trunk, review the diff, run "
            "CI on the merged result, resolve conflicts; the engine performs the git "
            "land on approval."
        )
        thread_id = _merge_thread_id(ws.key)
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
    if keep_stories:
        await conn.execute(
            "DELETE FROM git_node_stories gns "
            "USING unnest($1::text[], $2::text[]) AS cur(sid, nid) "
            "WHERE gns.story_id = cur.sid AND gns.git_node_id <> cur.nid",
            keep_stories, keep_nodes,
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
