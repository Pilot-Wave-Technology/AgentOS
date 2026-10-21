"""lanes — the trunk lanes a goal works on, per repo.

ONE REPOSITORY CAN CARRY SEVERAL TRUNK LANES IN ONE GOAL: a backend
branch and a frontend branch of one app, each its own merge target. The
lane a story lands on is the trunk resource it was GRANTED
(`repo_identity.resolve_ws_trunk`), and `git_nodes.trunk_branch` records
the one it actually got.

WHAT THIS REPLACES. `git_repos.trunk_branch` — one trunk per repo, the
legacy default kept for compatibility by `git_graph_per_goal_trunk.sql`
and explicitly NOT authoritative. Reporting it as "the repo's trunk"
gave a two-lane goal one lane's name and, worse, one lane's HEAD sha to
compare every branch against: a branch perfectly in sync with its own
lane read as `stale` because the OTHER lane had moved.

WHERE LANES COME FROM, in the order they are trusted:

  1. `git_nodes.trunk_branch` — lanes the engine is actually landing on.
  2. `goal_resources` `kind='branch' role='trunk'` — lanes the goal has
     declared but has not cut a branch on yet. Linked to the repo by
     (owner, name), case-insensitively, falling back to the PARENT repo
     resource's owner/name: a trunk row's own `repo_owner` is often
     empty, which is why `resolve_repo_trunk` matches by parentage.
  3. The repo's own `trunk_branch`, and ONLY for a repo that declared
     neither of the above. Adding it unconditionally is how a card comes
     to describe a third branch nobody works on.

`goal_resources.git_repo_id` is deliberately not used for the link: it
is written only for engine-cut FEATURE branches
(`branches.ensure_branch_resource`), so every GO-registered repo and
lane row has it NULL.
"""

from __future__ import annotations

_LANES_SQL = """
WITH repo AS (
    SELECT id, owner, name, trunk_branch
      FROM git_repos WHERE id = ANY($2::text[])
),
declared AS (
    SELECT b.branch_name AS trunk_branch,
           lower(coalesce(NULLIF(b.repo_owner, ''), p.repo_owner, '')) AS owner,
           lower(coalesce(NULLIF(b.repo_name,  ''), p.repo_name,  '')) AS name
      FROM goal_resources b
      LEFT JOIN goal_resources p ON p.id = b.parent_id
     WHERE b.goal_id = $1 AND b.kind = 'branch' AND b.role = 'trunk'
       AND coalesce(b.status, 'active') = 'active'
       AND coalesce(b.branch_name, '') <> ''
),
lane AS (
    SELECT n.git_repo_id, n.trunk_branch
      FROM git_nodes n
     WHERE n.goal_id = $1 AND coalesce(n.trunk_branch, '') <> ''
       AND n.git_repo_id = ANY($2::text[])
    UNION
    SELECT r.id, d.trunk_branch
      FROM declared d
      JOIN repo r ON lower(r.owner) = d.owner AND lower(r.name) = d.name
)
SELECT l.git_repo_id, l.trunk_branch,
       t.trunk_sha, t.trunk_sha_seen_at
  FROM lane l
  LEFT JOIN git_repo_trunks t
    ON t.git_repo_id = l.git_repo_id AND t.trunk_branch = l.trunk_branch
 ORDER BY l.git_repo_id, l.trunk_branch
"""


async def lanes_by_repo(conn, goal_id: str, repos) -> dict:
    """{git_repo_id: [{"trunk", "trunk_sha", "trunk_moved_at"}, ...]}.

    `repos` is the repo rows already fetched — each with `git_repo_id`
    and `trunk_branch` — so the default-lane fallback costs no query.
    Every repo passed in gets a key, so a caller never has to guard.
    """
    ids = [r["git_repo_id"] for r in repos]
    out: dict = {rid: [] for rid in ids}
    if not ids:
        return out
    for row in await conn.fetch(_LANES_SQL, goal_id, ids):
        out.setdefault(row["git_repo_id"], []).append({
            "trunk": row["trunk_branch"],
            "trunk_sha": row["trunk_sha"],
            "trunk_moved_at": row["trunk_sha_seen_at"],
        })
    # A repo that has declared nothing and cut nothing still has ONE
    # honest answer, and it is the only one there is to contradict.
    missing = [r for r in repos if not out.get(r["git_repo_id"])
               and (r["trunk_branch"] or "")]
    if missing:
        shas = {(s["git_repo_id"], s["trunk_branch"]): s
                for s in await conn.fetch(
                    "SELECT git_repo_id, trunk_branch, trunk_sha, "
                    "       trunk_sha_seen_at "
                    "  FROM git_repo_trunks WHERE git_repo_id = ANY($1::text[])",
                    [r["git_repo_id"] for r in missing])}
        for r in missing:
            s = shas.get((r["git_repo_id"], r["trunk_branch"]))
            out[r["git_repo_id"]] = [{
                "trunk": r["trunk_branch"],
                "trunk_sha": s["trunk_sha"] if s else None,
                "trunk_moved_at": s["trunk_sha_seen_at"] if s else None,
            }]
    return out
