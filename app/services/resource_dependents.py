"""What else a goal resource is holding up, asked BEFORE it is removed.

`delete_resource` was written for documents: soft-delete one row, drop the
GCS blob, pull the file out of the live workspaces. Repos and branches were
later added to the same table and the same endpoint, and nothing in that
path ever learned that a repo resource has dependents.

So deleting one half-succeeded in silence. The repo row was marked deleted;
its lane rows stayed `active` with `parent_id` pointing at a row that no
longer resolved; the stories granted those lanes stalled with nothing in any
log to say why. On goal GOAL1 that cost roughly a day of a stalled merge
lane and left four stories stranded.

`resolve_default_lane` now finds a lane without its repo row, so this is no
longer load-bearing for CORRECTNESS -- it is here so the operator is told
what they are about to detach instead of finding out later.
"""
from __future__ import annotations

# Kinds whose removal can strand something else. A document has no
# dependents; a repo carries lanes, and a lane can carry story grants.
_HAS_DEPENDENTS = ("repo", "branch", "gcp_environment")


async def active_children(conn, goal_id: str, resource_id: str) -> list[dict]:
    """Active rows whose `parent_id` is `resource_id`, outermost first."""
    rows = await conn.fetch(
        "SELECT id, kind, name, role, branch_name FROM goal_resources "
        " WHERE goal_id = $1 AND parent_id = $2 "
        "   AND coalesce(status, 'active') = 'active' "
        " ORDER BY kind, name",
        goal_id, resource_id,
    )
    return [dict(r) for r in rows]


async def granted_to_stories(conn, goal_id: str, resource_ids: list) -> list[dict]:
    """Unfinished stories holding a grant on any of `resource_ids`.

    A finished story's grant is history and does not argue against removing
    the resource; an unfinished one is work that will look for it.
    """
    if not resource_ids:
        return []
    rows = await conn.fetch(
        "SELECT DISTINCT s.id, s.title, s.state "
        "  FROM story_resources sr "
        "  JOIN stories s ON s.id = sr.story_id "
        " WHERE sr.resource_id = ANY($1::text[]) AND s.goal_id = $2 "
        "   AND s.state NOT IN ('done','reviewed','cancelled','merged') "
        " ORDER BY s.id",
        [str(r) for r in resource_ids], goal_id,
    )
    return [dict(r) for r in rows]


def describe_children(children: list[dict]) -> str:
    if not children:
        return ""
    names = ", ".join(
        (c.get("branch_name") or c.get("name") or c["id"]) for c in children[:6]
    )
    more = f" (+{len(children) - 6} more)" if len(children) > 6 else ""
    return f"{len(children)} active child resource(s): {names}{more}"


def describe_stories(stories: list[dict]) -> str:
    if not stories:
        return ""
    ids = ", ".join(s["id"] for s in stories[:6])
    more = f" (+{len(stories) - 6} more)" if len(stories) > 6 else ""
    return f"{len(stories)} unfinished story/stories granted it: {ids}{more}"


async def blocking_dependents(conn, goal_id: str, resource_id: str, kind: str) -> dict:
    """What stands in the way of removing this resource.

    Returns `children` (CASCADABLE -- the operator may archive them along
    with the parent) and `stories` (NEVER CASCADABLE).

    THE ASYMMETRY IS THE POINT. A child resource is ours to archive: it is
    bookkeeping under a row that is going away. A story's grant is not --
    it is the story's statement of where its work lands, and silently
    removing the thing it points at re-routes that work to whatever the
    fallback happens to be. That is the original failure in miniature, so
    there is no `cascade` that waves it through: the operator re-grants the
    story (`grant_resource` / `revoke_resource`, which their assistant
    holds) and then deletes.

    Both are gathered for the resource AND its children, because deleting a
    repo detaches the lanes and it is the LANES that stories are granted.
    Reporting only the repo's own grants would call a repo with four
    stranded lane-holders unencumbered.
    """
    if kind not in _HAS_DEPENDENTS:
        return {"children": [], "stories": []}
    children = await active_children(conn, goal_id, resource_id)
    stories = await granted_to_stories(
        conn, goal_id, [resource_id] + [c["id"] for c in children]
    )
    return {"children": children, "stories": stories}
