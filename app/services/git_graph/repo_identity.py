"""Canonical git-repo identity (plan §3.1, §8).

A real GitHub repo (owner/name) may be referenced by many goals' `goal_resources`
rows. The engine coordinates per *repo*, cross-goal, so it needs exactly ONE
canonical `git_repos` row per real repo, deduped by (owner, name). This module
maps the existing per-goal repo resources onto that canonical identity.

`canonical_repo_id` is a pure function (no I/O) so callers can compute a repo id
during read-only plan derivation without touching the database. `ensure_git_repo`
is the only writer here and is called only from the persisting reconcile path.
"""
import hashlib
import logging
from typing import Optional

import asyncpg

from .trunk_lane import resolve_default_lane, resolve_repo_default_branch

_log = logging.getLogger(__name__)


logger = logging.getLogger(__name__)


def canonical_repo_id(owner: str, name: str) -> str:
    """Deterministic, stable canonical id for a repo, identical across goals.

    GitHub treats owner/name case-insensitively for identity, so we lower-case
    before hashing. 16 hex chars (64 bits) is ample for dedup across the handful
    of repos a deployment touches; the ``gr_`` prefix keeps ids self-describing.
    """
    key = f"{(owner or '').strip().lower()}/{(name or '').strip().lower()}"
    return "gr_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# Resolve a story's repo via the SAME join the provisioning path uses
# (operator_provisioning._resolve_repo_for_story): story_resources → goal_resources,
# preferring an explicit feature branch (kind='branch') over the goal-wide repo.
_RESOLVE_STORY_REPO_SQL = """
    SELECT gr.id        AS resource_id,
           gr.kind,
           gr.role,
           gr.repo_owner,
           gr.repo_name,
           gr.clone_url,
           gr.default_branch,
           gr.branch_name,
           gr.base_branch
      FROM story_resources sr
      JOIN goal_resources gr ON gr.id = sr.resource_id
     WHERE sr.story_id = $1
       AND gr.goal_id = $2
       AND gr.status = 'active'
       AND gr.kind IN ('repo', 'branch')
       AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
       -- NEVER resolve a story to its repo's TRUNK designation: a role='trunk'
       -- branch resource exists only so the engine knows the trunk (resolve_repo_trunk)
       -- — it is not a working ref. Without this exclusion the trunk row (often created
       -- before the engine's feature branch) sorts first by created_at and the story
       -- resolves to e.g. `dev`, so work targets trunk directly.
       AND NOT (gr.kind = 'branch' AND gr.role = 'trunk'
                AND COALESCE(sr.relation, '') NOT IN
                    ('produces', 'modifies'))
     ORDER BY (gr.role = 'feature') DESC, (gr.kind = 'branch') DESC, gr.created_at ASC
     LIMIT 1
"""


# Multi-repo variant of the above: same join and same ordering, but WITHOUT the
# `LIMIT 1`, and carrying `sr.relation` so callers can split the story's repos into
# the WRITE set (relation 'produces'/'modifies' → gets a branch + node + PR) and the
# READ-ONLY context set (relation 'needs' → cloned for reference only, never landed).
# Ordering matches the single-repo query, with ONE addition: a WRITE relation sorts
# first. story_resources' PK includes `relation`, so one repo can hold both a 'needs'
# and a 'produces' row; without this the per-repo dedup picked between them arbitrarily
# and `is_write` for that repo was effectively a coin flip. Write is the stronger grant,
# so it wins. On a repo with a single grant the order is unchanged.
_RESOLVE_STORY_REPOS_SQL = """
    SELECT gr.id        AS resource_id,
           gr.kind,
           gr.role,
           gr.repo_owner,
           gr.repo_name,
           gr.clone_url,
           gr.default_branch,
           gr.branch_name,
           gr.base_branch,
           sr.relation
      FROM story_resources sr
      JOIN goal_resources gr ON gr.id = sr.resource_id
     WHERE sr.story_id = $1
       AND gr.goal_id = $2
       AND gr.status = 'active'
       AND gr.kind IN ('repo', 'branch')
       AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
       AND NOT (gr.kind = 'branch' AND gr.role = 'trunk'
                AND COALESCE(sr.relation, '') NOT IN
                    ('produces', 'modifies'))
     ORDER BY (sr.relation IN ('produces', 'modifies')) DESC,
              (gr.role = 'feature') DESC, (gr.kind = 'branch') DESC, gr.created_at ASC
"""

# A story's repo grant is a WRITE grant (branch + node + PR + merge) for these
# relations; anything else (notably 'needs') is read-only context. Kept as a module
# constant because provisioning (operator_provisioning) must split the same way — the
# two resolvers disagreeing would mean cloning a repo we never branch, or vice versa.
WRITE_RELATIONS = ("produces", "modifies")


def is_write_relation(relation: Optional[str]) -> bool:
    """True if this story_resources.relation grants WRITE access to the repo.

    An absent relation means the row came from the single-repo-goal fallback (which
    has no story_resources row at all) — that is the story's target repo, so it is a
    write grant.
    """
    if not relation:
        return True
    return relation.strip().lower() in WRITE_RELATIONS


async def resolve_story_repos(
    conn: asyncpg.Connection, story_id: str, goal_id: str
) -> list[dict]:
    """Return EVERY repo a story touches — one row per distinct repo — or [].

    Read-only. This is the multi-repo successor to `resolve_story_repo`; that
    function is now a shim over this one (see below). Each dict additionally carries
    `relation` and a derived `is_write` flag.

    De-duplication is per canonical repo (owner/name, case-insensitive), NOT per
    resource row: a story commonly holds both the bare `kind='repo'` grant and the
    engine-cut `kind='branch'` feature row for the same repo, and those are ONE repo
    with one branch — the query ordering puts the feature branch first, so the first
    row per repo wins. Without this collapse a single-repo story would look
    multi-repo and get two nodes.
    """
    rows = await conn.fetch(_RESOLVE_STORY_REPOS_SQL, story_id, goal_id)
    by_repo: dict[str, dict] = {}
    for row in rows:
        repo_key = canonical_repo_id(row["repo_owner"], row["repo_name"])
        # A WRITE-relation trunk-role grant is a LANE TARGET: the story
        # lands on that branch, and two lanes of one repo are two
        # targets (two nodes, one merge group -- the same multi-write
        # shape as two repos, restored from the pre-refactor model).
        lane = (row["branch_name"] or "" if row.get("role") == "trunk"
                and is_write_relation(row["relation"]) else "")
        key = f"{repo_key}@{lane}" if lane else repo_key
        if key in by_repo:
            continue
        entry = dict(row)
        entry["git_repo_id"] = repo_key
        entry["lane"] = lane
        entry["is_write"] = is_write_relation(row["relation"])
        by_repo[key] = entry
    # A lane entry ABSORBS the plain entry for the same repo: the plain
    # row (the engine's own gb_* feature branch, or the bare repo grant)
    # describes the same node the lane targets, and keeping both would
    # double-node a single-lane story.
    laned_repos = {e["git_repo_id"] for e in by_repo.values() if e["lane"]}
    resolved = [e for k, e in by_repo.items()
                if e["lane"] or e["git_repo_id"] not in laned_repos
                or not e["is_write"]]
    if resolved:
        _log.debug(
            "resolve_story_repos: story=%s goal=%s resolved %d repo(s) (%d write, %d read-only)",
            story_id, goal_id, len(resolved),
            sum(1 for r in resolved if r["is_write"]),
            sum(1 for r in resolved if not r["is_write"]),
        )
        return resolved
    # Engine-model fallback — see resolve_story_repo for the full rationale. Retained
    # in the LIST variant too: under the engine the GO registers the repo but does not
    # link a per-story branch, so on a single-repo goal EVERY code story would resolve
    # to zero repos and go unplaced without it. On a multi-repo goal the target is
    # genuinely ambiguous, so we return [] and let the story stay unplaced until the
    # grant is explicit.
    fallback = await _single_repo_goal_fallback(conn, goal_id)
    if fallback is None:
        _log.debug(
            "resolve_story_repos: story=%s goal=%s resolved no repos (no grant, "
            "no single-repo fallback) — story stays unplaced", story_id, goal_id,
        )
        return []
    fallback["git_repo_id"] = canonical_repo_id(
        fallback["repo_owner"], fallback["repo_name"]
    )
    fallback["relation"] = None
    fallback["is_write"] = True
    _log.debug(
        "resolve_story_repos: story=%s goal=%s resolved via single-repo-goal fallback (%s/%s)",
        story_id, goal_id, fallback["repo_owner"], fallback["repo_name"],
    )
    return [fallback]


async def _single_repo_goal_fallback(
    conn: asyncpg.Connection, goal_id: str
) -> Optional[dict]:
    """The goal's sole registered repo, or None if it has zero or 2+ of them.

    `LIMIT 2` is deliberate: we only need to know whether the count is exactly one.
    """
    repos = await conn.fetch(
        "SELECT id AS resource_id, kind, role, repo_owner, repo_name, clone_url, "
        "       default_branch, branch_name, base_branch "
        "FROM goal_resources "
        "WHERE goal_id = $1 AND kind = 'repo' AND status = 'active' "
        "  AND repo_name IS NOT NULL AND repo_name <> '' "
        "LIMIT 2",
        goal_id,
    )
    return dict(repos[0]) if len(repos) == 1 else None


async def resolve_story_repo(
    conn: asyncpg.Connection, story_id: str, goal_id: str
) -> Optional[dict]:
    """Return the goal_resources repo/branch row a story touches, or None.

    Read-only. The returned dict carries repo_owner/repo_name (→ canonical id),
    clone_url, default_branch, and any explicit branch_name/base_branch.

    SINGLE-REPO SHIM (multi-repo plan M1.1): kept for callers not yet migrated to
    `resolve_story_repos`. It preserves the historical "first row wins" behaviour
    exactly — same join, same ordering — so migrating a caller is a behaviour-neutral
    change on single-repo stories. New callers should use `resolve_story_repos`.
    """
    row = await conn.fetchrow(_RESOLVE_STORY_REPO_SQL, story_id, goal_id)
    if row:
        return dict(row)
    # Engine-model fallback (single-repo goals): under the engine the GO declares
    # deps + registers the repo but does NOT link a per-story branch (the engine
    # cuts + links that later), so a code story usually has no story_resources link
    # yet. If the goal has EXACTLY ONE registered repo, associate the story to it so
    # the engine can place + branch it. Multi-repo goals need an explicit per-story
    # target (not inferred here — returns None → story stays unplaced).
    #
    # ENGINE-ONLY: this resolver feeds plan derivation / reconcile. Provisioning has
    # its own resolver (_resolve_repo_for_story) which must NOT fall back to the raw
    # repo — it lazy-waits for the engine-cut branch instead.
    return await _single_repo_goal_fallback(conn, goal_id)


async def resolve_repo_trunk(
    conn: asyncpg.Connection, goal_id: str, owner: str, name: str
) -> str:
    """The trunk branch the engine cuts from / merges into for `owner/name` on this
    goal. Precedence (product decision): a declared `role='trunk'` branch
    resource wins; else the repo resource's `default_branch`; else 'main'.

    THE ONLY RESOLVER of this fact. It used to be one of two, kept
    deliberately in step with a GO-facing `_build_trunk_branch_map` in
    go_agent.py; that block stopped being assembled when GO moved to
    tools, and the function sat there uncalled while three docstrings
    went on citing it as authoritative. It is gone.

    A LANE IS MATCHED BY ITS OWN IDENTITY, parent repo only as a fallback,
    and NO repo resource is required -- `trunk_lane.resolve_default_lane`
    holds the query and the incident that motivated it. This previously
    read "matched by parentage, not by its own (possibly empty)
    `repo_owner`", which was true of the code and was the bug: a lane whose
    parent row had been deleted became undiscoverable and every ungranted
    story silently reverted to 'main'.

    ONE ANSWER, AND A GOAL CAN HAVE SEVERAL LANES. This returns the
    per-repo DEFAULT lane -- the one flagged `is_default_lane`, else oldest
    declared. Where the question is
    "which lane does this WORK land on", the answer is the lane the
    story was granted: `resolve_ws_trunk`, and `kernel/go/lanes.py` for
    the full set. A FEATURE branch resource's `default_branch`
    is incidental and never sets the trunk (sourcing it from whichever resource a
    story happened to resolve to is exactly what let 'main' override a configured
    'dev'). Owner match on the repo row is case-insensitive (GitHub identity is)."""
    # THE LANE IS ASKED FOR FIRST, AND WITHOUT A REPO ROW. This used to
    # require an active repo resource and then match lanes by `parent_id`
    # alone, so deleting the repo row returned the literal "main" without
    # ever reading the lanes -- see `trunk_lane` for the goal this broke
    # and why the branch rows can identify themselves.
    lane = await resolve_default_lane(conn, goal_id, owner, name)
    if lane:
        return lane
    # No lane declared. The repo resource's own default_branch is next, and
    # 'main' is the last resort for a goal that declared neither.
    return await resolve_repo_default_branch(conn, goal_id, owner, name) or "main"


async def resolve_ws_trunk(
    conn: asyncpg.Connection, goal_id: str, owner: str, name: str,
    story_ids: list,
) -> str:
    """The trunk a WORKSTREAM lands on: the trunk-lane resource its member
    stories were granted, else the goal's per-repo default.

    A repo may carry several trunk lanes in one goal (backend and
    frontend branches of one app repo, each its own merge target). The
    lane a story works in is chosen the same way everything else about a
    story is chosen -- by what GO granted it. All riders of one
    workstream must agree; disagreement is a mis-grant, said out loud
    and resolved conservatively to the per-repo default rather than
    silently picking a side.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT gr.branch_name
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = ANY($5::text[])
           AND gr.goal_id = $1 AND gr.kind = 'branch'
           AND gr.role = 'trunk' AND gr.status = 'active'
           AND gr.branch_name IS NOT NULL AND gr.branch_name <> ''
           AND (
                (gr.repo_name = $2
                 AND lower(coalesce(gr.repo_owner, '')) = lower($3))
             OR gr.parent_id IN (
                    SELECT id FROM goal_resources
                     WHERE goal_id = $1 AND kind = 'repo'
                       AND repo_name = $2
                       AND lower(coalesce(repo_owner, '')) = lower($4))
           )
        """, goal_id, name, owner, owner, [str(s) for s in story_ids])
    lanes = sorted({r["branch_name"] for r in rows})
    if len(lanes) == 1:
        return lanes[0]
    if len(lanes) > 1:
        logger.warning(
            "workstream stories %s straddle trunk lanes %s of %s/%s -- "
            "falling back to the per-repo trunk; regrant so all riders "
            "share one lane", story_ids, lanes, owner, name)
    return await resolve_repo_trunk(conn, goal_id, owner, name)


async def set_observed_trunk(executor, repo_id: str, trunk_branch: str, sha: str) -> None:
    """Upsert the observed trunk HEAD per (repo, trunk_branch) into git_repo_trunks
    (the per-goal-trunk cache, spec §0). Detection cache ONLY — never a mutation base
    (mutations always live-read). `executor` may be a pool OR a connection (both have
    `.execute`). No-op on an empty sha/branch."""
    if not sha or not trunk_branch:
        return
    await executor.execute(
        "INSERT INTO git_repo_trunks (git_repo_id, trunk_branch, trunk_sha, trunk_sha_seen_at) "
        "VALUES ($1, $2, $3, now()) "
        "ON CONFLICT (git_repo_id, trunk_branch) DO UPDATE "
        "  SET trunk_sha = EXCLUDED.trunk_sha, trunk_sha_seen_at = now()",
        repo_id, trunk_branch, sha,
    )


async def ensure_git_repo(
    conn: asyncpg.Connection,
    owner: str,
    name: str,
    *,
    clone_url: Optional[str] = None,
    trunk_branch: Optional[str] = None,
) -> str:
    """Idempotently upsert the canonical git_repos row; return its id.

    WRITES. Called only from the persisting reconcile path. Safe to call
    repeatedly: it fills identity fields but never clobbers protection config
    (merge_method/linear_history/required_checks/...) that the engine learns
    later from the GitHub API.
    """
    repo_id = canonical_repo_id(owner, name)
    await conn.execute(
        """
        INSERT INTO git_repos (id, owner, name, clone_url, trunk_branch)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (id) DO UPDATE SET
            clone_url    = COALESCE(NULLIF(EXCLUDED.clone_url, ''), git_repos.clone_url),
            trunk_branch = COALESCE(NULLIF(EXCLUDED.trunk_branch, ''), git_repos.trunk_branch),
            updated_at   = now()
        """,
        repo_id,
        owner,
        name,
        clone_url or "",
        trunk_branch or "main",
    )
    return repo_id
