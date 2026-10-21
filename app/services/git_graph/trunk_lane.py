"""Which trunk lane a repo's work lands on, found from the LANE, not the repo row.

WHY THIS EXISTS. A goal declares trunk lanes as `goal_resources` rows with
`kind='branch' role='trunk'`. Those rows carry their own `repo_owner` /
`repo_name`, so they identify themselves. But `resolve_repo_trunk` used to
find them only by `parent_id`, after first requiring an ACTIVE repo resource:

    repo = SELECT ... WHERE kind='repo' AND status='active' ...
    if repo is None:
        return "main"                      # <- never reached the lanes
    trunk = SELECT ... WHERE role='trunk' AND parent_id = repo["id"]

So deleting a repo resource did not orphan its lanes cosmetically -- it
severed the only path by which they were discovered, and the fallback was
the literal string "main". Measured on goal GOAL1 (2026-10-06): the goal had
declared `develop` as its trunk, the operator deleted the repo
row while trying to switch to it, and resolution silently reverted to `main`
-- the exact outcome the switch was meant to prevent.

THE DATA WAS ALREADY THERE. `kernel/go/lanes.py` has resolved lanes correctly
all along: branch's own identity first, parent repo only as a FALLBACK, a
LEFT JOIN so a missing parent cannot erase the row, and the parent's status
never consulted. This module is that same shape, for the single-answer case.

ONE ANSWER, AND A GOAL CAN HAVE SEVERAL LANES. This returns the per-repo
DEFAULT lane. Where the question is "which lane does this WORK land on", the
answer is the lane the story was granted -- `repo_identity.resolve_ws_trunk`,
and `kernel/go/lanes.py` for the full set.

THE FALLBACK IS NOT A FEATURE. This answers only for work that was granted
NO lane, and `resource_allocation` is explicit that reaching here is itself
the defect: "the story carries NO lane, and resolve_ws_trunk falls back to
the oldest lane: work lands on a branch nobody chose, silently, on exactly
the multi-lane goals the model exists for."

So the tie-break stays dumb -- oldest declared -- on purpose. A designated
`is_default_lane` column was built here and removed before release: making
the fallback configurable entrenches it, and imports a "one default trunk
per repo" idea that contradicts a model where a repo carries as many lanes
as the goal needs and each story is GRANTED the one it lands on. The fix
for work arriving here is the missing grant, not a better guess.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


# A lane is matched by its OWN (owner, name), falling back to its parent
# repo resource's. The join is LEFT and the parent's status is not
# consulted: a lane outlives the repo ROW that happened to introduce it.
_DEFAULT_LANE_SQL = """
    SELECT NULLIF(b.branch_name, '') AS branch_name
      FROM goal_resources b
      LEFT JOIN goal_resources p
             ON p.id = b.parent_id
            AND p.goal_id = b.goal_id
            AND p.kind = 'repo'
     WHERE b.goal_id = $1
       AND b.kind = 'branch'
       AND b.role = 'trunk'
       AND coalesce(b.status, 'active') = 'active'
       AND coalesce(b.branch_name, '') <> ''
       AND lower(coalesce(NULLIF(b.repo_name,  ''), p.repo_name,  '')) = lower($2)
       AND lower(coalesce(NULLIF(b.repo_owner, ''), p.repo_owner, '')) = lower($3)
     ORDER BY b.created_at ASC
     LIMIT 1
"""


async def resolve_default_lane(conn, goal_id: str, owner: str, name: str):
    """The goal's default trunk lane for `owner/name`, or None if none declared.

    Needs no repo resource. Returns None -- not a guess -- when the goal has
    declared no trunk lane for this repo, so the caller decides what the
    absence of a declaration means.
    """
    return await conn.fetchval(_DEFAULT_LANE_SQL, goal_id, name, owner)


# The repo RESOURCE's own `default_branch`, used only when no lane is
# declared. Restricted to an ACTIVE row on purpose: reading a field off a
# row the operator deleted would be resurrecting it. A deleted repo with no
# surviving lane falls through to the caller's last resort.
_REPO_DEFAULT_BRANCH_SQL = """
    SELECT NULLIF(default_branch, '')
      FROM goal_resources
     WHERE goal_id = $1
       AND kind = 'repo'
       AND status = 'active'
       AND repo_name = $2
       AND lower(coalesce(repo_owner, '')) = lower($3)
     ORDER BY created_at ASC
     LIMIT 1
"""


async def resolve_repo_default_branch(conn, goal_id: str, owner: str, name: str):
    """The repo resource's declared default branch, or None."""
    return await conn.fetchval(_REPO_DEFAULT_BRANCH_SQL, goal_id, name, owner)


async def lane_summary(conn, goal_id: str, owner: str, name: str) -> dict:
    """What this goal's work on `owner/name` actually lands on, for display.

    `{"lanes": [...], "effective": "main", "declared": bool}`.

    EXISTS SO A PAGE CAN SAY IT OUT LOUD. With no lane declared, work still
    goes somewhere -- the repo resource's `default_branch`, or `main` -- and
    nothing in the UI said which. An operator who removes the last lane sees
    an empty list and reasonably concludes the branch is no longer in use,
    while the engine goes on merging into it. Same shape as the bug this
    module exists for: the system quietly using a branch no resource names.
    """
    rows = await conn.fetch(
        "SELECT b.branch_name "
        "  FROM goal_resources b "
        "  LEFT JOIN goal_resources p "
        "         ON p.id = b.parent_id AND p.goal_id = b.goal_id AND p.kind = 'repo' "
        " WHERE b.goal_id = $1 AND b.kind = 'branch' AND b.role = 'trunk' "
        "   AND coalesce(b.status, 'active') = 'active' "
        "   AND coalesce(b.branch_name, '') <> '' "
        "   AND lower(coalesce(NULLIF(b.repo_name,  ''), p.repo_name,  '')) = lower($2) "
        "   AND lower(coalesce(NULLIF(b.repo_owner, ''), p.repo_owner, '')) = lower($3) "
        " ORDER BY b.created_at ASC",
        goal_id, name, owner,
    )
    lanes = [r["branch_name"] for r in rows]
    if lanes:
        return {"lanes": lanes, "effective": lanes[0], "declared": True}
    fallback = await resolve_repo_default_branch(conn, goal_id, owner, name)
    return {"lanes": [], "effective": fallback or "main", "declared": False}
