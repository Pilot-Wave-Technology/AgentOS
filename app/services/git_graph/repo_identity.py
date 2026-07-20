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

_log = logging.getLogger(__name__)


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
       AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')
     ORDER BY (gr.role = 'feature') DESC, (gr.kind = 'branch') DESC, gr.created_at ASC
     LIMIT 1
"""


async def resolve_story_repo(
    conn: asyncpg.Connection, story_id: str, goal_id: str
) -> Optional[dict]:
    """Return the goal_resources repo/branch row a story touches, or None.

    Read-only. The returned dict carries repo_owner/repo_name (→ canonical id),
    clone_url, default_branch, and any explicit branch_name/base_branch.
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


async def resolve_repo_trunk(
    conn: asyncpg.Connection, goal_id: str, owner: str, name: str
) -> str:
    """The trunk branch the engine cuts from / merges into for `owner/name` on this
    goal. Precedence (product decision): a `role='trunk'` branch resource whose
    `parent_id` points at the repo resource wins; else the repo resource's
    `default_branch`; else 'main'.

    Uses the SAME `parent_id` repo↔trunk linkage as the GO-facing
    `_build_trunk_branch_map` (go_agent.py), so the engine and the agent never
    disagree on the trunk — and a trunk branch is matched by parentage, not by its
    own (possibly empty) `repo_owner`. A FEATURE branch resource's `default_branch`
    is incidental and never sets the trunk (sourcing it from whichever resource a
    story happened to resolve to is exactly what let 'main' override a configured
    'dev'). Owner match on the repo row is case-insensitive (GitHub identity is)."""
    repo = await conn.fetchrow(
        "SELECT id, NULLIF(default_branch, '') AS default_branch FROM goal_resources "
        "WHERE goal_id = $1 AND kind = 'repo' AND status = 'active' "
        "  AND repo_name = $2 AND lower(coalesce(repo_owner, '')) = lower($3) "
        "ORDER BY created_at ASC LIMIT 1",
        goal_id, name, owner,
    )
    if repo is None:
        return "main"  # no repo resource (legacy/branch-only goal) — GitHub default
    trunk = await conn.fetchval(
        "SELECT NULLIF(branch_name, '') FROM goal_resources "
        "WHERE goal_id = $1 AND kind = 'branch' AND role = 'trunk' AND status = 'active' "
        "  AND parent_id = $2 AND branch_name IS NOT NULL AND branch_name <> '' "
        "ORDER BY created_at ASC LIMIT 1",
        goal_id, repo["id"],
    )
    return trunk or repo["default_branch"] or "main"


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
