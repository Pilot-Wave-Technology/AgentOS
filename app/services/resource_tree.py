"""Group goal resources into the two-level tree the browser page draws.

WHY IT IS ITS OWN MODULE. This was six lines inside `resource_browser`, and
those six lines decided that a row whose parent is absent from the result
set appears NOWHERE: roots were `not r["parent_id"]`, and the template
reaches a child only through `children_map[root.id]`, so such a row was
fetched, filed under a key nothing ever reads, and silently dropped.

On goal GOAL1 the repo resource was deleted and all three of its trunk
lanes left the page at once. `?show_archived=1` could not bring them back
-- it widens the filter to 'archived' and never to 'deleted' -- so the
operator could neither see the lanes nor archive one to move the trunk,
and the only repair left was SQL against the database.

A page that silently omits rows is worse than one that shows them
awkwardly, so an orphan is re-rooted and marked. Nothing changes when
parents are present, which is every other goal.
"""
from __future__ import annotations


def build_tree(resources: list[dict]) -> tuple[list[dict], dict]:
    """`(roots, children_map)` for `resources`, in the order given.

    A row is a root when it declares no parent, OR when the parent it
    declares is not in `resources` -- archived out of view, deleted, or
    belonging to another goal. Re-rooted rows get `orphaned_from` set to the
    parent id they wanted, which the template badges so the row reads as a
    problem rather than as a normal top-level resource.

    Mutates the dicts it is given (setting `orphaned_from`), which is what
    the caller wants: these are freshly built per request from `dict(row)`.
    """
    present = {r["id"] for r in resources}
    roots: list[dict] = []
    children_map: dict[str, list[dict]] = {}
    for r in resources:
        parent = r.get("parent_id")
        if not parent:
            roots.append(r)
        elif parent not in present:
            r["orphaned_from"] = parent
            roots.append(r)
        else:
            children_map.setdefault(parent, []).append(r)
    return roots, children_map
