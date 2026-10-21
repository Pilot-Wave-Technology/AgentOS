"""The DIGEST half of the brief: what LANDED, and where.

Split from `resource_brief` (which keeps the IDENTITY half) so each file
stays readable and neither grows a second subject. Identity is an LLM
card about what a resource IS, rebuilt rarely; a digest is one appended
row per landing, written with no model call from what the engine already
knows.

A DIGEST BELONGS TO A LANE. One repository can carry several trunk lanes
in one goal, each its own merge target; "something landed in this
repository" is true of both lanes and useful about neither. See
`_digest_target`.
"""

from __future__ import annotations

import json
import logging

from app.services.resource_brief import _touches_identity

_log = logging.getLogger(__name__)


async def _changed_paths(owner: str, name: str, base: str,
                         head: str) -> list:
    """Best effort, one call, never fatal. Absent beats invented."""
    try:
        import httpx

        from app.github_tokens import generate_installation_token
        tok = await generate_installation_token([name])
        if isinstance(tok, dict):
            tok = tok.get("token", "")
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"https://api.github.com/repos/{owner}/{name}/compare/"
                f"{base}...{head}",
                headers={"Authorization": f"Bearer {tok}",
                         "Accept": "application/vnd.github+json"})
            if r.status_code != 200:
                return []
            return [f["filename"] for f in r.json().get("files", [])][:50]
    except Exception:                                       # noqa: BLE001
        _log.info("resource_digest: compare unavailable for %s/%s", owner,
                  name)
        return []


async def _digest_target(pool, goal_id: str, owner: str, name: str,
                         trunk: str) -> str:
    """Which resource card a landing is written onto.

    THE LANE, when the goal declared one. A digest records what landed
    WHERE; written onto the repo row it says only "something landed in
    this repository", which on a two-lane goal is true of both lanes and
    informative about neither -- while the lane cards, the ones a
    planner actually reads, keep saying nothing has landed.

    Matched by (owner, name) case-insensitively, the way
    `resolve_repo_trunk` matches, with the parent repo row's identity
    standing in for a trunk row's often-empty own `repo_owner`.

    NOT by `goal_resources.git_repo_id`, which is what this used to do:
    that column is written only for engine-cut FEATURE branches
    (`branches.ensure_branch_resource`), so on a GO-registered repo it
    is NULL and the lookup returned nothing -- no digest was recorded
    for anything, anywhere, and the function said so by returning False.
    """
    if trunk:
        lane = await pool.fetchval(
            """
            SELECT b.id FROM goal_resources b
              LEFT JOIN goal_resources p ON p.id = b.parent_id
             WHERE b.goal_id = $1 AND b.kind = 'branch' AND b.role = 'trunk'
               AND coalesce(b.status, 'active') = 'active'
               AND b.branch_name = $4
               AND lower(coalesce(NULLIF(b.repo_owner, ''), p.repo_owner, ''))
                   = lower($2)
               AND lower(coalesce(NULLIF(b.repo_name, ''), p.repo_name, ''))
                   = lower($3)
             ORDER BY b.created_at ASC LIMIT 1
            """, goal_id, owner or "", name or "", trunk)
        if lane:
            return lane
    return await pool.fetchval(
        "SELECT id FROM goal_resources WHERE goal_id = $1 AND kind = 'repo' "
        "  AND coalesce(status, 'active') = 'active' "
        "  AND lower(coalesce(repo_owner, '')) = lower($2) "
        "  AND lower(coalesce(repo_name, '')) = lower($3) "
        "ORDER BY created_at ASC LIMIT 1",
        goal_id, owner or "", name or "") or ""


async def record_landing(pool, goal_id: str, node_id: str) -> bool:
    """Append one digest row for a landed node. Idempotent per node."""
    n = await pool.fetchrow(
        """
        SELECT gn.id, gn.branch_name, gn.trunk_branch, gn.merge_commit_sha,
               gn.pre_merge_trunk_sha, gr.owner, gr.name
          FROM git_nodes gn JOIN git_repos gr ON gr.id = gn.git_repo_id
         WHERE gn.id = $1 AND gn.goal_id = $2
        """, node_id, goal_id)
    if n is None:
        return False
    resource_id = await _digest_target(
        pool, goal_id, n["owner"], n["name"], n["trunk_branch"] or "")
    if not resource_id:
        _log.info("resource_digest: no resource card for %s/%s in goal %s; "
                  "landing of %s not digested", n["owner"], n["name"],
                  goal_id, node_id)
        return False
    already = await pool.fetchval(
        "SELECT 1 FROM resource_summaries WHERE resource_id = $1 "
        "AND summary_type = 'digest' AND metadata->>'node' = $2",
        resource_id, node_id)
    if already:
        return False

    stories = [r["story_id"] for r in await pool.fetch(
        "SELECT story_id FROM git_node_stories WHERE git_node_id = $1 "
        "ORDER BY seq", node_id)]
    paths = []
    if n["merge_commit_sha"] and n["pre_merge_trunk_sha"]:
        paths = await _changed_paths(n["owner"], n["name"],
                                     n["pre_merge_trunk_sha"],
                                     n["merge_commit_sha"])
    sha = (n["merge_commit_sha"] or "")[:12]
    # NAME THE LANE. "landed" without a destination is the same sentence
    # for both lanes of a two-lane repo.
    lane = n["trunk_branch"] or ""
    line = (f"{sha} landed `{n['branch_name']}`"
            + (f" → `{lane}`" if lane else "")
            + f" — stories: {', '.join(stories) or '-'}"
            + (f" — paths: {', '.join(paths[:15])}" if paths else ""))
    await pool.execute(
        """
        INSERT INTO resource_summaries
            (resource_id, goal_id, summary_type, content, metadata)
        VALUES ($1, $2, 'digest', $3, $4)
        """,
        resource_id, goal_id, line,
        json.dumps({"node": node_id, "sha": sha, "stories": stories,
                    "lane": lane, "paths": paths}))
    if paths and _touches_identity(paths):
        # The card's own subject changed; the digest cannot absorb that.
        await pool.execute(
            "UPDATE goal_resources SET summary_stale = true WHERE id = $1",
            resource_id)
    _log.info("resource_digest: digest row for %s (%s)", resource_id,
              node_id)
    return True
