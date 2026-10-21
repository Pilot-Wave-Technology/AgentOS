"""code_state — what the code is doing, without reading any of it.

The engine computes every drift signal a planner needs — observed trunk
HEAD, per-branch sync state, mergeability, conflicting paths, PR state —
and until this tool, GO could see none of it. This is the drift organ:
per TRUNK LANE, per live branch, bytes.

PER LANE, not per repo. One repository can carry several trunk lanes in
one goal, each its own merge target, and a branch is only ever stale
against the lane it lands on. See `lanes.py`.

WHAT IS DELIBERATELY ABSENT: file contents, ahead/behind commit COUNTS
(the engine measures them during a grind pass and does not persist them
— reporting a number nobody stored would be invention; `synced_to` vs
`trunk` shas plus `stale` says the same thing honestly), and anything an
executor should read in its own workspace instead.
"""

from __future__ import annotations

from .lanes import lanes_by_repo

# Node states that no longer occupy a lane.
NODE_TERMINAL = ("MERGED", "ABANDONED")


def _paths(value) -> list:
    """conflict_paths is jsonb, which asyncpg returns as a STRING unless
    a codec is registered. Treating it as a list "worked" only on NULL —
    joining a raw JSON string iterates its characters. Parse honestly."""
    if not value:
        return []
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return list(value) if isinstance(value, (list, tuple)) else [str(value)]



async def code_state(conn, goal_id: str) -> dict:
    repos = await conn.fetch(
        """
        SELECT DISTINCT gr.id AS git_repo_id, gr.owner, gr.name,
               gr.trunk_branch
          FROM git_repos gr
         WHERE gr.id IN (SELECT git_repo_id FROM git_nodes WHERE goal_id = $1
                         UNION
                         SELECT git_repo_id FROM goal_resources
                          WHERE goal_id = $1 AND git_repo_id IS NOT NULL)
         ORDER BY gr.owner, gr.name
        """,
        goal_id)
    # PER LANE, not per repo: `git_repos.trunk_branch` is the legacy
    # single default, and comparing every branch against ITS sha marks a
    # branch stale because a lane it never touches has moved.
    lanes = await lanes_by_repo(conn, goal_id, repos)

    nodes = await conn.fetch(
        """
        SELECT gn.id, gn.git_repo_id, gn.branch_name, gn.state,
               gn.trunk_branch, gn.mergeable, gn.conflict_paths,
               gn.pr_number, gn.last_synced_trunk_sha, gn.last_error,
               gn.updated_at
          FROM git_nodes gn
         WHERE gn.goal_id = $1
         ORDER BY (gn.state = ANY($2::text[])), gn.updated_at DESC
         LIMIT 40
        """,
        goal_id, list(NODE_TERMINAL))
    riders = await conn.fetch(
        """
        SELECT gns.git_node_id, gns.seq, gns.story_id,
               s.state, s.assigned_to, s.title
          FROM git_node_stories gns
          JOIN stories s ON s.id = gns.story_id
         WHERE gns.git_node_id = ANY($1::text[])
         ORDER BY gns.git_node_id, gns.seq
        """,
        [n["id"] for n in nodes])
    riders_by_node: dict = {}
    for r in riders:
        riders_by_node.setdefault(r["git_node_id"], []).append(r)

    # Keyed on (repo, LANE). A node compares against the lane it lands
    # on -- `git_nodes.trunk_branch`, the same column the engine cuts and
    # merges by -- so the two never disagree about what "stale" means.
    trunk_sha = {(rid, l["trunk"]): l["trunk_sha"]
                 for rid, ls in lanes.items() for l in ls}

    out_nodes = []
    for n in nodes:
        node_riders = riders_by_node.get(n["id"], [])
        # The chain, in handoff order — and WHO is next. Three stories
        # riding one branch is exactly the case a planner misreads
        # without this: the first two being done delivers nothing.
        current = next((r for r in node_riders
                        if r["state"] not in ("done", "cancelled",
                                              "rejected")), None)
        lands_on = n["trunk_branch"] or ""
        t_sha = trunk_sha.get((n["git_repo_id"], lands_on))
        out_nodes.append({
            "node": n["id"], "repo": n["git_repo_id"],
            "branch": n["branch_name"], "state": n["state"],
            # WHICH LANE this branch is cut from and merges back into.
            # Without it a two-lane repo reads as one pile of branches.
            "lands_on": lands_on or None,
            "riders": [{"seq": r["seq"], "story": r["story_id"],
                        "state": r["state"],
                        "assigned_to": r["assigned_to"] or None}
                       for r in node_riders],
            "current_rider": current["story_id"] if current else None,
            # Honest drift: the sha this branch last synced to, the sha
            # trunk is at now, and whether they differ. No invented
            # commit counts.
            "synced_to": n["last_synced_trunk_sha"],
            "stale": bool(t_sha and n["last_synced_trunk_sha"]
                          and t_sha != n["last_synced_trunk_sha"]),
            "mergeable": n["mergeable"],
            "conflict_paths": _paths(n["conflict_paths"]),
            "pr": n["pr_number"],
            "last_error": n["last_error"],
        })

    return {
        "goal": goal_id,
        "repos": [{"repo": r["git_repo_id"],
                   "name": f'{r["owner"]}/{r["name"]}',
                   "lanes": lanes.get(r["git_repo_id"], [])}
                  for r in repos],
        "nodes": out_nodes,
        "live": sum(1 for n in out_nodes
                    if n["state"] not in NODE_TERMINAL),
    }
