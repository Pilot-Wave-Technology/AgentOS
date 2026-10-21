"""Which workspace directory holds a story's plan documents.

A story's plan lives at `<root>/stories/<story_id>/plans/cycle-NN-<slug>.md`, where
`<root>` is the clone directory the plan was written into. Two places need that root and
must agree exactly: the plan-write gate (`internal_assistant.validate_plan`, which tells
the agent where to write and rejects anywhere else) and the plan commit
(`plan_approval._push_plan_to_repo`, which strips the root to get a repo-relative path).
Disagreement means either the agent is rejected for writing exactly where it was told, or
an approved plan is silently never committed.

**Read the recorded path; do not re-derive it.** Both `stories.current_plan_path` and
`plan_versions.plan_doc_path` already store the full path INCLUDING the root, so the
answer exists as data the moment a story has a plan. `primary_write_subdir` computed it
instead — as the alphabetically-first write repo — which has two consequences a stored
value does not:

  * granting a repository that sorts earlier silently relocates where future plans are
    expected, so the gate starts rejecting the path the agent was previously told to use;
  * two files must keep matching orderings for the gate and the commit to agree, a
    coupling `_clone_descriptors` warns about in its own docstring.

This is the same principle `migrations/story_workspace_nodes.sql` states for `subdir` —
*"recomputing it would silently point at the wrong directory for every pre-existing
story"* — applied to the plan root. Derivation survives only as the first-plan fallback,
where there is genuinely nothing recorded yet.

Read-only. Nothing here writes, and the root is therefore frozen by the act of the first
plan being written rather than by any explicit freeze.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# The fixed segment between the clone root and the story's own plan folder. Splitting on
# it is what makes a multi-repo root work: `repos/acme__web` CONTAINS a slash, so taking
# the first path segment would return `repos` and every plan path would be wrong.
_STORIES_SEGMENT = "/stories/"


def plan_root_of(plan_path: str | None) -> str:
    """The clone root a recorded plan path sits under, or "" if it has none.

    Returns "" for a plan written at the workspace root (`plans/…`) — a shape the gate
    already rejects — so callers fall through to the layout instead of inventing a root.
    """
    path = (plan_path or "").strip()
    if _STORIES_SEGMENT not in path:
        return ""
    root = path.split(_STORIES_SEGMENT, 1)[0].strip("/")
    return root


async def _root_still_exists(pool, story_id: str, root: str) -> bool:
    """Is `root` still one of this story's recorded clone directories?

    A recorded plan root is authoritative right up until the directory it names stops
    existing. Provisioning now puts every repo under `repos/<owner>__<name>`, so an
    operator story re-provisioned from the old single-`repo/` layout has its clone in a
    new place while `stories.current_plan_path` still points at the old one. Returning
    the stale root would put the validate-plan gate and the CLAUDE.md the agent is
    reading in different directories — the agent writes where it was told and the gate
    rejects it.

    A story with NO recorded clones is LEGACY: there is nothing to check against, and
    its workspace really is at `repo/`, so the recorded root stands. That is the same
    "empty means legacy, not missing" rule the rest of the layout code follows.
    """
    from app.services.story_workspace_layout import resolve_workspace_clones  # noqa: PLC0415
    try:
        clones = await resolve_workspace_clones(pool, story_id)
    except Exception:
        return True  # never invalidate a good root because a lookup failed
    if not clones:
        return True
    return any(c.get("subdir") == root for c in clones)


async def resolve_plan_root(pool, story_id: str) -> str:
    """The directory holding this story's plans, preferring what was recorded.

    Order of preference, most authoritative first:

    1. `stories.current_plan_path` — where this story's live plan actually is.
    2. The newest `plan_versions.plan_doc_path` — covers a story mid-replan, where the
       current pointer can be cleared while previous versions still record the root.
    3. `primary_write_subdir` — the story has no plan yet, so there is nothing to read and
       the workspace layout is the only available answer.

    A story that already has a plan therefore keeps its plan root for life, whatever
    happens to its repository grants afterwards.
    """
    try:
        recorded = await pool.fetchval(
            "SELECT current_plan_path FROM stories WHERE id = $1", story_id,
        )
        root = plan_root_of(recorded)
        if root and await _root_still_exists(pool, story_id, root):
            return root

        recorded = await pool.fetchval(
            """SELECT plan_doc_path FROM plan_versions
                WHERE story_id = $1 AND COALESCE(plan_doc_path, '') <> ''
                ORDER BY version DESC LIMIT 1""",
            story_id,
        )
        root = plan_root_of(recorded)
        if root and await _root_still_exists(pool, story_id, root):
            _log.debug(
                "story_plan_root: story=%s root %r taken from a prior plan version "
                "(current_plan_path is empty)", story_id, root,
            )
            return root
    except Exception:
        # Degrade to derivation rather than failing the gate or the commit. Both callers
        # treat this as a path prefix, and the derived answer is what they used before
        # this module existed, so the worst case is the previous behaviour.
        _log.warning(
            "story_plan_root: could not read the recorded plan path for story=%s — "
            "falling back to deriving it from the workspace layout",
            story_id, exc_info=True,
        )

    from app.services.operator_provisioning import primary_write_subdir  # noqa: PLC0415
    derived = await primary_write_subdir(pool, story_id)
    _log.debug(
        "story_plan_root: story=%s has no recorded plan — derived root %r", story_id, derived,
    )
    return derived
