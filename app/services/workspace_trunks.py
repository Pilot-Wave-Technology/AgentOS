"""Stamp each recorded clone with the trunk it was cut from.

WHY IT WAS MISSING. `story_workspace_nodes.trunk_branch` has existed all
along and `local_hydrate` already serves it to the extension as `trunk` --
but `_record_story_workspace_nodes` never wrote it, so every row in both
lanes carried `''`. The extension's review prompt branches on that value
and fell through to its degraded form: "diffing this branch against the
trunk it was cut from. If you cannot establish the trunk, say so".

What the reviewer then did on goal VSCT1 (2026-10-06) is the reason this
matters. It had no `main`, no trunk ref in the clone at all, and instead of
answering `code_not_reviewed` as instructed it fell back to the repository's
ROOT COMMIT and diffed against that. On a two-commit test repo the output
looks plausible. On a real repository it presents the entire codebase as the
story's work, and every judgement in the review is then made against the
wrong question.

ONE TRUNK PER CLONE, NOT PER STORY. A story can hold several repos, and one
repository can carry several trunk lanes in a goal -- so the answer is per
(repo, branch) row, which is exactly the grain `story_workspace_nodes`
already uses. `resolve_ws_trunk` is the resolver that reads the lane a
story was GRANTED, falling back to the goal's per-repo default; using it
here keeps the recorded trunk and the engine's merge target in step,
because they are then the same function of the same grants.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


async def annotate_trunks(conn, goal_id: str, story_id: str,
                          clones: list[dict]) -> list[dict]:
    """Return `clones` with `trunk_branch` filled in per repo.

    Mutates and returns the dicts it is given -- they are built fresh per
    call by both writers. A clone that already carries a non-empty
    `trunk_branch` is left alone, so a caller that knows better than the
    grants (the engine, recording a node it has already cut) can say so.

    NEVER RAISES. A missing trunk degrades the review to "say you could not
    establish it", which is a correct and honest outcome; failing the
    checkout over it would be a much worse trade.
    """
    if not clones or not goal_id:
        return clones

    from app.services.git_graph.repo_identity import resolve_ws_trunk

    for clone in clones:
        if (clone.get("trunk_branch") or "").strip():
            continue
        owner = clone.get("repo_owner") or ""
        name = clone.get("repo_name") or ""
        if not name:
            clone["trunk_branch"] = ""
            continue
        try:
            trunk = await resolve_ws_trunk(
                conn, goal_id, owner, name, [story_id],
            )
        except Exception:  # noqa: BLE001 — see NEVER RAISES above
            _log.warning(
                "workspace_trunks: could not resolve a trunk for %s/%s on "
                "story %s — the review will be told it has no base",
                owner, name, story_id, exc_info=True,
            )
            trunk = ""
        # A clone cut FROM its own branch has no diff base worth naming:
        # that is the lane itself being worked on directly, and
        # `trunk...HEAD` would be empty. Left blank so the prompt says so
        # rather than printing a diff of nothing and calling it the story.
        clone["trunk_branch"] = "" if trunk == clone.get("branch_name") else (trunk or "")

    _log.info(
        "workspace_trunks: story=%s -> %s", story_id,
        ", ".join(
            f"{c.get('repo_name')}@{c.get('branch_name')}"
            f"<-{c.get('trunk_branch') or '(none)'}" for c in clones
        ),
    )
    return clones
