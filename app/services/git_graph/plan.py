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

from .repo_identity import canonical_repo_id, resolve_story_repos

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
    # G5: merge-per-story is the default. True = this story JOINS its
    # in-repo dependency's branch (the old chain shape), for chains whose
    # intermediate states must not reach trunk.
    land_together: bool = False
    # ALL repos the story touches, write and read-only (multi-repo plan M1.4). Each
    # entry is a resolve_story_repos() dict (git_repo_id, repo_owner, repo_name,
    # clone_url, default_branch, branch_name, base_branch, relation, is_write).
    #
    # M2 LANDED: derivation now switches over to `write_repos` for a multi-write story —
    # see the `multi_write` fan-out below, which emits one workstream per write repo.
    # `repo_id` above remains the single derivation target ONLY for a single-write story,
    # so derive_plan stays behaviour-preserving for it. Defaults to () so existing
    # StoryInput constructions stay valid.
    repos: tuple[dict, ...] = ()

    @property
    def write_repos(self) -> tuple[dict, ...]:
        """The repos that get a branch + node + PR (relation produces/modifies)."""
        return tuple(r for r in self.repos if r.get("is_write"))

    @property
    def context_repos(self) -> tuple[dict, ...]:
        """The repos cloned for reference only (relation 'needs') — never landed."""
        return tuple(r for r in self.repos if not r.get("is_write"))


@dataclass
class Workstream:
    """A branch: an ordered chain of stories that ship as a unit (§4 rule 1)."""
    key: str                         # stable: "<repo_id>:<head_story_id>"
    repo_id: str
    goal_id: str
    branch_name: str                 # feat/<goalId>-<headStoryId>
    story_ids: list[str]             # in handoff order (head first)
    owners: list[str]                # distinct owners in handoff order
    # Which MERGE GROUP this workstream lands in. Normally the workstream's own key
    # (1 workstream : 1 group : 1 merge story, the historical shape). For a
    # multi-write story every one of its N workstreams shares ONE group key, so the
    # story gets a single merge group of N nodes and a single merge story — the whole
    # point of §11.6. Defaults to `key` so single-repo group ids never change.
    group_key: str = ""
    # Position of this workstream's node inside its merge group
    # (git_merge_group_nodes.seq) — the intra-group landing order. Always 0 for a
    # single-node group, which is what every single-repo workstream is.
    group_seq: int = 0
    # The trunk LANE this workstream lands on ("" = resolve per repo/grants).
    lane: str = ""

    def __post_init__(self):
        if not self.group_key:
            self.group_key = self.key


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

    # MULTI-WRITE stories (research/MULTI_REPO_STORIES_PLAN.md §11.6). A story that
    # writes to 2+ repos gets one node per repo, bound by a single merge group.
    #
    # THE RULE: a multi-write story is ALWAYS a workstream head and NEVER shares a
    # workstream with another story. That is what makes its N branches exclusive to it
    # (decision #8) — and, critically, it means node identity does not change: the key
    # stays `<repo_id>:<head>`, which is already distinct per repo, so no existing node
    # is re-keyed and no backfill migration is needed.
    # A write TARGET is (repo, lane) -- two lanes of one repo are two
    # targets exactly like two repos (restored pre-refactor multi-write,
    # now lane-aware). Lane is "" for the classic single-trunk shape, so
    # every existing key and branch name is preserved verbatim.
    multi_write: dict[str, tuple] = {}
    for s in placeable:
        targets = tuple(sorted({
            (r["git_repo_id"], r.get("lane") or "")
            for r in s.write_repos if r.get("git_repo_id")
        }))
        if len(targets) > 1:
            multi_write[s.story_id] = targets
    if multi_write:
        _log.info(
            "derive_plan: goal=%s has %d multi-write story/stories: %s",
            goal_id, len(multi_write),
            ", ".join(f"{sid[:8]}→{len(rids)} repos" for sid, rids in multi_write.items()),
        )

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
        """G5: EVERY story is a head — one branch per story, merged when it
        completes, deleted after landing — unless it explicitly opts into
        its predecessor's branch with land_together.

        The old default was the inverse: a maximal in-repo chain shared
        one branch and merged once at the end, so three stories done
        meant zero delivered until the third finished. That shape is
        still available, per chain, where intermediate states genuinely
        must not reach trunk — it just stopped being what depends_on
        implicitly means.

        A land_together story is still a head when it does not UNIQUELY
        continue a chain (zero or several in-repo predecessors, or a
        predecessor with several successors) or when the predecessor is
        multi-write — a multi-write story owns its branches outright
        (§11.6) and absorbs no one.
        """
        if sid in multi_write:
            return True
        if not by_id[sid].land_together:
            return True
        ps = preds[sid]
        if len(ps) != 1:
            return True
        if ps[0] in multi_write:
            return True
        return len(succs[ps[0]]) != 1

    # Build workstreams by walking each head's maximal linear chain forward.
    for head in sorted(s.story_id for s in placeable if is_head(s.story_id)):
        chain = [head]
        cur = head
        # A multi-write head absorbs no successors — it is a workstream of one in every
        # repo it writes to (§11.6).
        while head not in multi_write and len(succs[cur]) == 1:
            nxt = succs[cur][0]
            # Only a land_together successor joins this branch; is_head
            # already encodes that, so the walk stops at the first story
            # that wants its own branch — which is now the default.
            if preds[nxt] != [cur] or is_head(nxt):
                break
            chain.append(nxt)
            cur = nxt

        # One workstream per repo the head writes to. Single-write stories yield
        # exactly one, keyed and named exactly as before — this loop is behaviour-
        # preserving for them.
        head_targets = multi_write.get(head)
        if head_targets is None:
            single = next(iter(by_id[head].write_repos), None)
            head_iter = ((by_id[head].repo_id,
                          (single or {}).get("lane") or ""),)
        else:
            head_iter = head_targets
        # Lane-qualify the key and the branch ONLY where two targets
        # share a repo -- single-lane and multi-repo stories keep their
        # historical keys, so no existing node is re-keyed.
        repo_counts: dict[str, int] = {}
        for rid, _lane in head_iter:
            repo_counts[rid] = repo_counts.get(rid, 0) + 1
        for group_seq, (repo_id, lane) in enumerate(head_iter):
            qualify = lane and repo_counts.get(repo_id, 0) > 1
            lane_slug = lane.replace("/", "-") if lane else ""
            ws = Workstream(
                key=(f"{repo_id}@{lane}:{head}" if qualify
                     else f"{repo_id}:{head}"),
                repo_id=repo_id,
                goal_id=goal_id,
                branch_name=(f"feat/{goal_id}-{head}-{lane_slug}" if qualify
                             else f"feat/{goal_id}-{head}"),
                story_ids=chain,
                owners=[],
                group_key=(f"story:{head}" if head_targets
                           else f"{repo_id}:{head}"),
                group_seq=group_seq,
                lane=lane,
            )
            for sid in chain:
                # story_to_ws maps a story to ONE workstream and is used for
                # cross-workstream dependency gating. For a multi-write story the
                # first repo (sorted, hence deterministic) is the representative;
                # its merge group covers all N nodes, so gating on any one of them
                # gates the whole set.
                plan.story_to_ws.setdefault(sid, ws.key)
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

    # One merge per merge GROUP: handoff if any workstream in the group is depended
    # on, else ship. For single-repo work group_key == key, so this is exactly the
    # previous "one merge per workstream". For a multi-write story its N workstreams
    # collapse to one merge — a single human approval lands all its repos.
    _group_reason: dict[str, str] = {}
    _group_order: list[str] = []
    for w in plan.workstreams:
        if w.group_key not in _group_reason:
            _group_order.append(w.group_key)
            _group_reason[w.group_key] = "ship"
        if w.key in depended_on:
            _group_reason[w.group_key] = "handoff"
    plan.merges = [
        MergeStoryPlan(workstream_key=gk, reason=_group_reason[gk])
        for gk in _group_order
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

# AN AUTONOMOUS ERRAND IS NOT THE ENGINE'S. It never lands -- that has
# always been true -- and D1 gave it a branch anyway "so that it has
# somewhere to work", with the exclusion happening later, at the merge
# lane. For an errand somebody starts by hand and watches, that is
# right: they may well want the diff.
#
# An AUTONOMOUS one is a different animal. Nobody is watching, its
# deliverable is the artifacts it emits, and the branch it never merges
# is one more dead ref and one more row in a topology a person reads to
# understand what is in flight. It showed as a workstream on the board
# with a merge target it would never reach.
#
# So they are excluded HERE, at the read, rather than inside
# `derive_plan`. That function is pure and `work_kind`-free by contract
# -- `test_errand_never_lands` asserts it twice -- and it should stay
# that way: what the engine derives is a function of the stories it is
# given, and WHICH stories it is given is this function's business.
#
# HAND-STARTED ERRANDS ARE UNAFFECTED, which is the whole point of the
# join: `approved_by IS NOT NULL` is what makes a grant autonomous, so
# an errand with no live grant keeps its node exactly as before.
_AUTONOMOUS_ERRANDS = """
    SELECT DISTINCT s.story_id
      FROM kernel.errand_schedules s
     WHERE s.goal_id = $1
       AND s.story_id IS NOT NULL
       AND s.approved_by IS NOT NULL
       AND s.revoked_at IS NULL
"""


async def _autonomous(conn, goal_id: str) -> set:
    """Stories the engine must not derive a topology for.

    Fails OPEN -- an unreadable schedule table means the engine behaves
    as it did before autonomous errands existed, which is a dead branch
    rather than a missing one.
    """
    try:
        rows = await conn.fetch(_AUTONOMOUS_ERRANDS, goal_id)
    except Exception:                                       # noqa: BLE001
        _log.warning("git plan: could not read errand grants for %s; "
                       "deriving every story", goal_id, exc_info=True)
        return set()
    return {r["story_id"] for r in rows}


async def load_inputs(conn, goal_id: str) -> list[StoryInput]:
    """Read the goal's stories and resolve each one's repo. Read-only."""
    rows = await conn.fetch(
        "SELECT id, assigned_to, depends_on, state, story_kind, created_by, "
        "title, land_together FROM stories WHERE goal_id = $1",
        goal_id,
    )
    skip = await _autonomous(conn, goal_id)
    if skip:
        _log.info("git plan: %s has %d autonomous errand(s); the engine "
                    "derives no topology for them", goal_id, len(skip))
        rows = [r for r in rows if r["id"] not in skip]
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
        # Resolve the FULL repo set once, then take its first WRITE entry as the legacy
        # scalar target. resolve_story_repos preserves resolve_story_repo's ordering, so
        # on a single-repo story `repo` here is byte-identical to what the old call
        # returned. `repo_id`/`repo` stay single-repo ALWAYS — for a multi-write story,
        # M2's fan-out below derives from `inputs[i].repos` (all_repos, set on the
        # StoryInput a few lines down), not from this scalar.
        all_repos = await resolve_story_repos(conn, r["id"], goal_id)
        write_repos = [x for x in all_repos if x.get("is_write")]
        # FALLBACK — do NOT remove. `grant_resource`'s default relation is 'needs', so a
        # great many perfectly ordinary single-repo stories hold ONLY a 'needs' grant on
        # their repo. The pre-multi-repo resolver did not look at `relation` at all and
        # placed them regardless; filtering to write grants here made every one of them
        # resolve to no repo and be SILENTLY UNPLACED by the engine — no node, no branch,
        # no merge story — with the feature flag off.
        #
        # So when a story resolves repos but none is a write grant, fall back to the
        # first resolved repo, exactly as before. This is derivation-only: the write/
        # read-only split still governs provisioning, diff/commit and credential scoping,
        # where an explicit 'needs' grant genuinely means read-only.
        repo = write_repos[0] if write_repos else (all_repos[0] if all_repos else None)
        if all_repos and not write_repos:
            _log.debug(
                "load_inputs: story=%s has no write-relation repo grant; placing it on "
                "%s/%s anyway (pre-multi-repo behaviour)",
                r["id"][:8], repo["repo_owner"], repo["repo_name"],
            )
        if len(write_repos) > 1:
            _log.info(
                "load_inputs: story=%s has %d write repos; legacy scalar target is %s/%s "
                "(the multi_write fan-out below derives from write_repos, not this field)",
                r["id"][:8], len(write_repos), repo["repo_owner"], repo["repo_name"],
            )
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
                land_together=bool(r["land_together"]),
                repos=tuple(all_repos),
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
