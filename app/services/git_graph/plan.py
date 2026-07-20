"""Deterministic plan derivation (plan §4) — the engine's "brain".

Pure graph algorithm, no LLM, no branch mutation. Given a goal's stories
(`depends_on` + resolved repo + owner), it derives:

  * workstreams  — a workstream is a BRANCH that one or more stories ride
                   (plan §4 rule 1). The default grouping collapses a maximal
                   *linear dependent chain within one repo* into a single
                   workstream, regardless of owner (owners hand off on the same
                   branch). A merge is NOT created between consecutive stories on
                   the same branch.
  * merge stories — exactly one per workstream, at the trunk handoff: 'handoff'
                   if another workstream depends on this branch's output, else
                   'ship' (plan §4 rule 4). Multi-repo merge GROUPS (1:N) are a
                   later concern — today each story maps to one repo, so every
                   group is a single node.
  * cross-workstream deps — when a story depends on a story in a *different*
                   workstream (incl. a different repo — the cross-repo declared
                   gate, plan §4 rule 5), the consumer workstream depends on the
                   producer's MERGE (so the producer is on trunk before the
                   consumer's branch is lazily cut).

This module is READ-ONLY: `compute_plan` loads inputs and returns the derived
plan as data (a safe "preview"). Persisting it (creating git_nodes / merge
stories / groups) is the reconcile path, added in the next build step.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from app.database import get_pool

from .repo_identity import canonical_repo_id, resolve_story_repo

_log = logging.getLogger(__name__)

# Stories in these states are not part of the live git topology.
_TERMINAL_EXCLUDED = ("cancelled", "rejected")


@dataclass(frozen=True)
class StoryInput:
    """One story's derivation inputs (read from the DB, immutable thereafter)."""
    story_id: str
    owner: str                       # stories.assigned_to (uid); '' if unassigned
    repo_id: Optional[str]           # canonical git_repo_id; None if no repo resolved
    repo_owner: str
    repo_name: str
    clone_url: str                   # for ensure_git_repo (reconcile)
    default_branch: str              # repo trunk branch (reconcile)
    depends_on: tuple[str, ...]      # story ids
    state: str
    story_kind: str                  # 'code' | 'merge'
    created_by: str                  # 'engine' for engine-authored merge/diff/fix stories
    title: str
    landed: bool = False             # member of a MERGED node → work is on trunk, off live topology


@dataclass
class Workstream:
    """A branch: an ordered chain of stories that ship as a unit (§4 rule 1)."""
    key: str                         # stable: "<repo_id>:<head_story_id>"
    repo_id: str
    goal_id: str
    branch_name: str                 # feat/<goalId>-<headStoryId>
    story_ids: list[str]             # in handoff order (head first)
    owners: list[str]                # distinct owners in handoff order


@dataclass
class MergeStoryPlan:
    """One merge per workstream, at a trunk handoff or ship point (§4 rule 4)."""
    workstream_key: str
    reason: str                      # 'handoff' | 'ship'


@dataclass
class DerivedPlan:
    goal_id: str
    workstreams: list[Workstream] = field(default_factory=list)
    merges: list[MergeStoryPlan] = field(default_factory=list)
    story_to_ws: dict[str, str] = field(default_factory=dict)     # story_id -> ws key
    ws_deps: dict[str, list[str]] = field(default_factory=dict)   # ws key -> producer ws keys
    unplaced: list[str] = field(default_factory=list)             # code stories with no repo
    errors: list[str] = field(default_factory=list)               # validation failures

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "valid": self.valid,
            "errors": self.errors,
            "unplaced": self.unplaced,
            "workstreams": [
                {
                    "key": w.key,
                    "repo_id": w.repo_id,
                    "branch_name": w.branch_name,
                    "story_ids": w.story_ids,
                    "owners": w.owners,
                    "depends_on_workstreams": self.ws_deps.get(w.key, []),
                    "merge_reason": next(
                        (m.reason for m in self.merges if m.workstream_key == w.key), None
                    ),
                }
                for w in self.workstreams
            ],
        }


# ── pure derivation ──────────────────────────────────────────────────────────

def derive_plan(goal_id: str, stories: list[StoryInput]) -> DerivedPlan:
    """Pure function: stories → workstreams + merges + cross-workstream deps.

    Deterministic — every ordering is by story_id so the same input always
    yields the same workstream keys, branch names, and merge set.
    """
    plan = DerivedPlan(goal_id=goal_id)
    by_id = {s.story_id: s for s in stories}

    # Only HUMAN/GO code stories that are not terminal participate in topology.
    # Engine-authored stories are EXCLUDED: merge stories (story_kind='merge') and
    # the engine's own diff/fix stories (story_kind='code' but created_by='engine')
    # — the latter would otherwise be derived into their OWN workstream + a
    # merge-of-the-diff (spec §0.1, the cost of keeping diff/fix as 'code').
    # LANDED stories are EXCLUDED too: their node already MERGED, so their work is
    # on trunk — off the live topology. Without this the engine re-derives + no-op
    # merges already-shipped stories (e.g. a goal onboarded after its work merged via
    # the old pipeline), and a NEW story depending only on landed work would chain
    # onto the dead workstream instead of rooting a fresh branch off the updated trunk.
    code = [
        s for s in stories
        if s.story_kind == "code" and s.state not in _TERMINAL_EXCLUDED
        and s.created_by != "engine"
        and not s.landed
    ]
    placeable = [s for s in code if s.repo_id]
    plan.unplaced = sorted(s.story_id for s in code if not s.repo_id)
    placeable_ids = {s.story_id for s in placeable}

    # In-repo adjacency (only edges where both endpoints share a repo collapse a
    # chain; cross-repo edges become cross-workstream merge gates below).
    preds: dict[str, list[str]] = {s.story_id: [] for s in placeable}
    succs: dict[str, list[str]] = {s.story_id: [] for s in placeable}
    for s in placeable:
        for dep in s.depends_on:
            p = by_id.get(dep)
            if p is None or p.story_id not in placeable_ids:
                continue
            if p.repo_id == s.repo_id:
                preds[s.story_id].append(p.story_id)
                succs[p.story_id].append(s.story_id)
    for d in (preds, succs):
        for k in d:
            d[k].sort()

    def is_head(sid: str) -> bool:
        """A story starts a new workstream unless it uniquely continues a chain:
        exactly one in-repo predecessor whose only in-repo successor is this story."""
        ps = preds[sid]
        if len(ps) != 1:
            return True
        return len(succs[ps[0]]) != 1

    # Build workstreams by walking each head's maximal linear chain forward.
    for head in sorted(s.story_id for s in placeable if is_head(s.story_id)):
        chain = [head]
        cur = head
        while len(succs[cur]) == 1:
            nxt = succs[cur][0]
            if preds[nxt] != [cur] or is_head(nxt):
                break
            chain.append(nxt)
            cur = nxt

        repo_id = by_id[head].repo_id
        ws = Workstream(
            key=f"{repo_id}:{head}",
            repo_id=repo_id,
            goal_id=goal_id,
            branch_name=f"feat/{goal_id}-{head}",
            story_ids=chain,
            owners=[],
        )
        for sid in chain:
            plan.story_to_ws[sid] = ws.key
            owner = by_id[sid].owner
            if owner and (not ws.owners or ws.owners[-1] != owner):
                ws.owners.append(owner)
        plan.workstreams.append(ws)

    plan.workstreams.sort(key=lambda w: w.key)

    # Cross-workstream dependencies → which workstreams need a 'handoff' merge.
    ws_deps: dict[str, set[str]] = defaultdict(set)
    depended_on: set[str] = set()
    for s in placeable:
        cws = plan.story_to_ws.get(s.story_id)
        if cws is None:
            continue  # left unplaced by a cycle (validate reports it)
        for dep in s.depends_on:
            p = by_id.get(dep)
            if p is None:
                continue
            pws = plan.story_to_ws.get(p.story_id)
            if pws is not None and pws != cws:
                ws_deps[cws].add(pws)
                depended_on.add(pws)
    plan.ws_deps = {k: sorted(v) for k, v in ws_deps.items()}

    # One merge per workstream: handoff if a downstream workstream needs it, else ship.
    plan.merges = [
        MergeStoryPlan(
            workstream_key=w.key,
            reason="handoff" if w.key in depended_on else "ship",
        )
        for w in plan.workstreams
    ]

    plan.errors = validate_plan(stories, plan)
    return plan


def validate_plan(stories: list[StoryInput], plan: DerivedPlan) -> list[str]:
    """Acyclic + valid-refs validation over the code-story DAG (plan §4 layer 1).

    Cross-repo dependencies are honored when *declared* (they become merge
    gates); UNDECLARED cross-repo semantic deps are the conceded residual limit
    (plan §15 L12(d)) and cannot be detected here.
    """
    errors: list[str] = []
    by_id = {s.story_id: s for s in stories}
    code = {
        s.story_id: s for s in stories
        if s.story_kind == "code" and s.state not in _TERMINAL_EXCLUDED
        and s.created_by != "engine"
        and not s.landed
    }

    # 1. Dangling refs.
    for s in code.values():
        for dep in s.depends_on:
            if dep not in by_id:
                errors.append(
                    f"story {s.story_id} depends_on '{dep}' which does not exist in goal"
                )

    # 2. Acyclic (Kahn over the code-story subgraph; edges dep → story).
    indeg = {sid: 0 for sid in code}
    adj: dict[str, list[str]] = defaultdict(list)
    for s in code.values():
        for dep in s.depends_on:
            if dep in code:
                adj[dep].append(s.story_id)
                indeg[s.story_id] += 1
    queue = [sid for sid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if seen != len(code):
        in_cycle = sorted(sid for sid, d in indeg.items() if d > 0)
        errors.append(f"dependency cycle among stories: {in_cycle}")

    # 3. A placeable story left without a workstream means it sat in a cycle.
    for s in stories:
        if (
            s.story_kind == "code"
            and s.state not in _TERMINAL_EXCLUDED
            and s.created_by != "engine"
            and not s.landed
            and s.repo_id
            and s.story_id not in plan.story_to_ws
        ):
            errors.append(f"story {s.story_id} could not be placed in a workstream (cycle?)")

    return errors


# ── read-only loading + preview ───────────────────────────────────────────────

async def load_inputs(conn, goal_id: str) -> list[StoryInput]:
    """Read the goal's stories and resolve each one's repo. Read-only."""
    rows = await conn.fetch(
        "SELECT id, assigned_to, depends_on, state, story_kind, created_by, title "
        "FROM stories WHERE goal_id = $1",
        goal_id,
    )
    # Stories that are members of an already-MERGED node = "landed" (work is on
    # trunk). Marked so derive_plan drops them from the live topology.
    landed_rows = await conn.fetch(
        "SELECT DISTINCT gns.story_id FROM git_node_stories gns "
        "JOIN git_nodes n ON n.id = gns.git_node_id "
        "WHERE n.goal_id = $1 AND n.state = 'MERGED'",
        goal_id,
    )
    landed_ids = {r["story_id"] for r in landed_rows}
    inputs: list[StoryInput] = []
    for r in rows:
        repo = await resolve_story_repo(conn, r["id"], goal_id)
        repo_id = (
            canonical_repo_id(repo["repo_owner"], repo["repo_name"]) if repo else None
        )
        inputs.append(
            StoryInput(
                story_id=r["id"],
                owner=r["assigned_to"] or "",
                repo_id=repo_id,
                repo_owner=repo["repo_owner"] if repo else "",
                repo_name=repo["repo_name"] if repo else "",
                clone_url=(repo.get("clone_url") or "") if repo else "",
                default_branch=(repo.get("default_branch") or "main") if repo else "main",
                depends_on=tuple(r["depends_on"] or ()),
                state=r["state"],
                story_kind=r["story_kind"] or "code",
                created_by=r["created_by"] or "",
                title=r["title"],
                landed=r["id"] in landed_ids,
            )
        )
    return inputs


async def compute_plan(goal_id: str) -> DerivedPlan:
    """Load inputs and derive the plan. READ-ONLY — writes nothing, safe to call
    as a preview. Persisting reconcile is a separate (later) build step."""
    pool = get_pool()
    async with pool.acquire() as conn:
        stories = await load_inputs(conn, goal_id)
    return derive_plan(goal_id, stories)
