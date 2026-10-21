"""Record what a LOCAL checkout actually has on disk, in `story_workspace_nodes`.

The gateway provisioner records its clones there (`operator_provisioning.
_record_story_workspace_nodes`, called from `provision_story_workspace`), but that
path refuses local stories, so a locally-checked-out story had **no node rows at
all**. Every multi-repo-aware consumer reads that table and was written to degrade
to "single repo" when it is empty — deliberately, to keep the flag-off path
unchanged — so for local stories they silently degraded instead of being correct:

  * `app/routers/git_credential.py` `_resolve_story_repo_scope` raised
    **HTTP 404 "story ... has no provisioned repos"** for ANY local story. This is
    the one hard failure and the main reason this module exists.
  * `diff_review._story_write_repo_count` returned 0 rather than 1. Behaviourally
    identical today (every caller treats <= 1 the same) but wrong, and it makes the
    count useless as a signal.

**`subdir` is `repo`, and that is not arbitrary.** The VS Code extension's
`materialize()` clones into a hardcoded directory named `repo` under the story's
workspace dir — it does not use the gateway's `repos/<owner>__<name>` convention.
`story_workspace_nodes.subdir` documents the PHYSICAL on-disk path (see the 2NF note
in `migrations/story_workspace_nodes.sql`), so it must record what the extension
really did, not what the gateway would have done. `migrations/story_workspace_nodes
.sql` already anticipates this value: *"Legacy single-repo workspaces ... use
'repo'"*.

**Exactly one node — unless the calling client declared `multi-repo` (N-repo local
execution, research/NREPO_LOCAL_EXECUTION_PLAN.md, C7).** Without that capability, a
story is still refused up front (`local_checkout._import_readiness` /
`story_repo_scope`) if it has more than one live repo, so this module continues to
record exactly one node for every client that predates the capability. WITH it, this
records one row per live repo — always under `repos/<owner>__<name>`, never the
legacy `repo` directory: that convention only ever meant one thing, "the sole repo",
and cannot describe a workspace with more than one.

**Why this does NOT change any dispatch prompt.** Populating the table makes
`dag_ingest._resolve_story_repo_aliases` able to resolve aliases, which could set
`tasks.repo_subdir` and cause `build_dispatch_message` to emit
"Repository: `repo/` — cd repo/ ...". That instruction would be WRONG locally: the
extension opens the repo directory itself as the workspace folder, so the agent's cwd
is already inside `repo`. `dag_ingest` therefore skips alias resolution for local
stories — see the guard there, which cites this module.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.services.git_graph.repo_identity import canonical_repo_id

_log = logging.getLogger(__name__)

# The LEGACY clone directory: what the extension has always hardcoded
# (`materialize()` → path.join(destDir, "repo")). Every workspace created before the
# path contract sits here, and every story whose provenance we cannot establish is
# recorded here — see `_subdir_for` for why that is the safe default.
LOCAL_CLONE_SUBDIR = "repo"

# Whether a brand-new local workspace MAY use the gateway's `repos/<owner>__<name>`
# layout instead of the legacy single `repo` directory.
#
# This is a kill switch, not the decision. Even with it on, the layout is only used for a
# client that DECLARES it reads each repo's directory from the hydration bundle — see
# `_new_clone_subdir`. Recording `repos/<owner>__<name>` for a client that still joins a
# literal "repo" would describe a directory the client never creates, and all six readers
# would then resolve to a path that does not exist.
USE_REPOS_LAYOUT_FOR_NEW_WORKSPACES = True


def _new_clone_subdir(owner: str, name: str, *, path_contract: bool) -> str:
    """The subdir a NEW local workspace should use.

    TWO conditions, and both are required:

      * the kill switch above is on, and
      * ``path_contract`` — the calling client declared it resolves the clone directory
        from the bundle rather than hardcoding one.

    The second is the load-bearing one. The control plane cannot upgrade an installed
    extension, and a version string tells you which build is calling rather than what it
    supports, so the only safe signal is the client saying so. Absent that declaration the
    legacy directory is not a fallback but the correct answer: it is what that client will
    actually create on disk.

    Delegates to the gateway's `repo_clone_subdir` rather than re-deriving the naming, so
    local and gateway workspaces cannot drift into two conventions for the same repo — the
    drift `_clone_descriptors` warns about ("a disagreement between any two of them sends
    the assistant to a directory that isn't there").
    """
    if not (USE_REPOS_LAYOUT_FOR_NEW_WORKSPACES and path_contract):
        return LOCAL_CLONE_SUBDIR
    from app.services.operator_provisioning import repo_clone_subdir  # noqa: PLC0415
    return repo_clone_subdir(owner, name)


def _subdir_for(repo: dict, *, first_checkout: Optional[bool], path_contract: bool) -> str:
    """Which directory this story's clone lives in.

    Three cases, in order of certainty:

    1. **A row already exists** — handled by the caller, which never overwrites it. A
       clone on disk is never moved by a convention change.
    2. **This is the story's FIRST checkout** — nothing exists on disk yet, so the new
       convention is free to apply.
    3. **Unknown, or not the first checkout** — assume LEGACY.

    Case 3 is the one that matters. A story with no row that has been checked out
    before was worked under the old code, so its clone is sitting in `repo/` with the
    session's bridge token, MCP config, agent bundle, memory snapshot and possibly
    uncommitted work. Recording the new convention for it would point every consumer at
    a directory that does not exist, and the extension would clone fresh alongside it,
    orphaning that work. Guessing legacy is always recoverable — the worst case is a
    brand-new story getting the old layout, which still works — so the ambiguity is
    resolved toward the harmless answer rather than the destructive one.
    """
    if first_checkout is True:
        return _new_clone_subdir(
            repo["owner"], repo["repo"], path_contract=path_contract,
        )
    return LOCAL_CLONE_SUBDIR


async def _resolve_subdir(
    pool, story_id: str, repo: dict, *, first_checkout: Optional[bool],
    path_contract: bool, force_repos_layout: bool, intended_subdir: str = "",
) -> str:
    """The subdir for ONE repo, preserving whatever is already on disk for it.

    NEVER move a repo that is already on disk. `subdir` records a PHYSICAL path, and
    `migrations/story_workspace_nodes.sql` warns in as many words that recomputing it
    "would silently point at the wrong directory for every pre-existing story". The
    caller's writer is delete-then-insert, so without this lookup a later change to
    LOCAL_CLONE_SUBDIR would silently repoint every existing checkout — including one
    holding uncommitted work. Keyed on (story, repo) NOT the branch: a re-cut changes
    the branch but never the directory.

    `force_repos_layout` is set for a genuinely multi-repo checkout (C7): the legacy
    single `repo` directory cannot describe more than one repo, so there is no
    "assume legacy" fallback available the way `_subdir_for` has for the single-repo
    case — every NEW row goes under `repos/<owner>__<name>` regardless of
    `first_checkout`/`path_contract`. An EXISTING row still wins outright, same as
    always; this only changes what a brand-new row gets.
    """
    git_repo_id = canonical_repo_id(repo["owner"], repo["repo"])
    # KEYED ON THE LANE, NOT THE BRANCH. By (story, repo) alone, two LANES of
    # one repository resolve to one row and so to one directory, which the
    # extension refuses outright ("two clones resolved to the same
    # directory"). By BRANCH it would be worse: a re-cut changes the branch
    # and would then relocate a clone that is already on disk, which is the
    # one thing this lookup exists to prevent. The lane is the distinction --
    # a re-cut keeps it, a second lane does not.
    #
    # A row recorded before `trunk_branch` was written carries '', and still
    # matches: for those the old (story, repo) behaviour is the right answer,
    # because a workspace from that era has only ever had one clone per repo.
    # An exact lane match is preferred when both are on offer.
    existing_subdir = await pool.fetchval(
        "SELECT subdir FROM story_workspace_nodes "
        "WHERE story_id = $1 AND git_repo_id = $2 AND subdir <> '' "
        "  AND ($3 = '' OR coalesce(trunk_branch, '') IN ('', $3)) "
        "ORDER BY (coalesce(trunk_branch, '') = $3) DESC LIMIT 1",
        story_id, git_repo_id, repo.get("trunk") or "",
    )
    if intended_subdir:
        # Decided set-aware by `assign_subdirs` -- whether a lane needs an
        # `@<lane>` suffix depends on how many lanes of its repo the story
        # writes, which no per-repo call can know.
        intended = intended_subdir
    elif force_repos_layout:
        from app.services.operator_provisioning import repo_clone_subdir  # noqa: PLC0415
        intended = repo_clone_subdir(repo["owner"], repo["repo"])
    else:
        intended = _subdir_for(repo, first_checkout=first_checkout, path_contract=path_contract)
    subdir = existing_subdir or intended
    if existing_subdir and existing_subdir != intended:
        _log.info(
            "local_workspace_nodes: story=%s repo=%s/%s keeps its recorded subdir %r "
            "(the convention for a new workspace would be %r) — the clone is already "
            "on disk there", story_id, repo["owner"], repo["repo"],
            existing_subdir, intended,
        )
    elif not existing_subdir:
        _log.info(
            "local_workspace_nodes: story=%s first record for %s/%s — subdir=%r "
            "(first_checkout=%r, path_contract=%s, force_repos_layout=%s)",
            story_id, repo["owner"], repo["repo"], subdir, first_checkout, path_contract,
            force_repos_layout,
        )
    return subdir


async def record_local_workspace_node(
    pool, story_id: str, *, first_checkout: Optional[bool] = None,
    path_contract: bool = False, multi_repo: bool = False,
) -> bool:
    """Record every repo a local checkout has cloned. Returns True if written.

    NOT best-effort. The ABSENCE of a row is the system-wide "legacy workspace" flag,
    so a silently-missing row makes a new workspace read as legacy for the rest of its
    life. Callers must treat False as "the workspace is not established" —
    `build_hydration_bundle` refuses to serve a repo on False, which is what guarantees
    a workspace is never cloned unrecorded.

    `first_checkout` is the story's provenance, captured by `claim_checkout` BEFORE its
    own UPDATE overwrites `checked_out_at`. None means "unknown" and is treated as
    legacy — see `_subdir_for`.

    `multi_repo`, when true, resolves and records EVERY live repo (`_resolve_live_repos`)
    rather than the single-repo shim `_repo_ref` — the caller (`build_hydration_bundle`)
    is responsible for only setting this once it has ALSO confirmed the granted set is
    complete (no repo whose branch is still cutting), because unlike the gateway
    provisioner this has no later re-provisioning pass to catch a partial set — a local
    checkout is a one-shot client action (see research/NREPO_LOCAL_EXECUTION_PLAN.md §1.2
    for the fuller comparison to MULTI_REPO_STORIES_PLAN.md §12's operator-lane race).
    """
    from app.services.local_hydrate import _repo_ref, _resolve_live_repos  # noqa: PLC0415

    repos = await _resolve_live_repos(pool, story_id) if multi_repo else (
        [r] if (r := await _repo_ref(pool, story_id)) else []
    )
    if not repos:
        # No branch cut yet (or, without multi_repo, ambiguous — `_repo_ref` refuses
        # several live repos). Nothing truthful to record; the next hydrate retries.
        _log.info(
            "local_workspace_nodes: story=%s has no resolvable repo(s) yet — "
            "no node recorded", story_id,
        )
        return False

    # DIRECTORIES ARE DECIDED OVER THE WHOLE SET, by the same rule the gateway
    # lane uses (`story_clone_layout.assign_subdirs`), so the two lanes cannot
    # drift into two conventions for one repository. Whether a lane needs an
    # `@<lane>` suffix is a property of how many lanes of its repo the story
    # writes -- one lane keeps `repos/<owner>__<name>`, which is the path every
    # existing story already uses; two lanes each get the suffix.
    from app.services.story_clone_layout import assign_subdirs  # noqa: PLC0415
    sources = [{
        "repo_owner": r["owner"], "repo_name": r["repo"],
        "relation": "modifies", "kind": "branch", "role": "feature",
        "branch_name": r["branch"], "base_branch": r.get("trunk") or "",
    } for r in repos]
    assign_subdirs(sources)

    clones = []
    for repo, source in zip(repos, sources):
        subdir = await _resolve_subdir(
            pool, story_id, repo, first_checkout=first_checkout,
            path_contract=path_contract, force_repos_layout=len(repos) > 1,
            # ONLY for a genuinely multi-clone checkout -- the same
            # condition `force_repos_layout` uses. A single clone keeps the
            # legacy decision in `_subdir_for` untouched, because that is
            # what is already on disk for every existing story and a
            # convention change must never relocate one.
            intended_subdir=(source.get("subdir") or "") if len(repos) > 1 else "",
        )
        clones.append({
            "git_repo_id": canonical_repo_id(repo["owner"], repo["repo"]),
            "branch_name": repo["branch"],
            "subdir": subdir,
            "is_write": True,          # the local session commits and pushes this repo
            "repo_owner": repo["owner"],
            "repo_name": repo["repo"],
            # STRAIGHT FROM THE NODE. The engine recorded which lane it cut
            # this branch from, which is the authoritative answer and the only
            # one that is right per BRANCH -- re-deriving it per repo gives a
            # two-lane story one trunk for both clones, which is how
            # 7f995412's frontend clone came to be stamped with the backend's.
            "trunk_branch": repo.get("trunk") or "",
        })

    # Reuse the gateway's writer rather than duplicating the SQL: it is already
    # generic (takes a clone list, no gateway coupling) and owns the delete-then-
    # insert semantics that keep a story's node set consistent. Passing the FULL
    # current live set (not incremental) is what makes delete-then-insert correct
    # here too — same precondition the gateway path relies on.
    # THE DIFF BASE, resolved PER REPO. A story can hold several repos and a
    # repo can carry several trunk lanes, so this is a per-(repo, branch)
    # answer -- the grain `story_workspace_nodes` already uses. Without it
    # the local review has no base and guesses; see `workspace_trunks`.
    from app.services.workspace_trunks import annotate_trunks  # noqa: PLC0415
    goal_id = await pool.fetchval(
        "SELECT goal_id FROM stories WHERE id = $1", story_id)
    async with pool.acquire() as conn:
        await annotate_trunks(conn, goal_id or "", story_id, clones)

    from app.services.operator_provisioning import (  # noqa: PLC0415
        _record_story_workspace_nodes,
    )
    await _record_story_workspace_nodes(pool, story_id, clones)
    _log.info(
        "local_workspace_nodes: recorded story=%s — %d repo(s): %s",
        story_id, len(clones),
        ", ".join(f"{c['repo_owner']}/{c['repo_name']}@{c['branch_name']}"
                  f"<-{c.get('trunk_branch') or '(no base)'}({c['subdir']})"
                  for c in clones),
    )
    return True
