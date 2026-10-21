"""Plan approval flow (SHCA Phase 4 Stage 3-d/3-e).

On human approve:
  1. Read the latest plan file from the story workspace via gateway RPC.
     Recognized locations (most-preferred first):
       - repo/stories/<story_id>/plans/cycle-NN-<slug>.md  (current)
       - repo/docs/plans/<name>.md                        (prior)
       - docs/plans/<name>.md                              (legacy, workspace root)
  2. Run dag_ingest to validate + persist the DAG
  3. Push the plan to git so the team can see it (failure to push is
     non-fatal — plan ingestion still succeeds; SHCA surfaces a warning).
     Push only happens for paths under repo/.
  4. Trigger task_controller wake so the first ready subtask dispatches

On reject (with feedback):
  Just record the rejection; the human continues iterating with Claude
  in chat. No task rows touched.
"""
from __future__ import annotations

import logging
import re

from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError
from app.services.dag_ingest import ingest_approved_plan
from app.services.dag_parser import DagParseError


_log = logging.getLogger(__name__)


# Match plan path references. CANONICAL convention (one everywhere — planning
# skill, CLAUDE.md, validate-plan hook):
#   repo/stories/<story_id>/plans/cycle-NN-<slug>.md
# Also accepted for transition/legacy safety: the interim `plan_<name>` filename in
# the same stories/.../plans/ dir, and the pre-059 repo/docs/plans/ + workspace-root
# docs/plans/ forms.
# The clone root is `repos/<owner>__<name>` for every story now, but `repo` must stay
# accepted forever: stories provisioned before the layout became universal still have
# their plans there, and `resolve_plan_root` deliberately keeps a story on its recorded
# root for life. Note `"repos/x".startswith("repo/")` is FALSE, so the old grammar did
# not merely prefer the legacy shape — it rejected the new one outright.
_REPO_ROOT = r"(?:repos/[A-Za-z0-9_.-]+|repo)"
_PLAN_REF_RE = re.compile(
    rf"(?:{_REPO_ROOT}/stories/[A-Za-z0-9_.-]+/plans/(?:cycle-\d+-|plan_)[A-Za-z0-9_.-]+\.md"
    rf"|{_REPO_ROOT}/docs/plans/[A-Za-z0-9_.-]+\.md"
    r"|docs/plans/[A-Za-z0-9_.-]+\.md)",
)


def _strip_repo_root(path: str) -> str | None:
    """Path relative to its clone root, or None if it sits under no clone root.

    Accepts both shapes so one prefix test covers every story: the universal
    `repos/<owner>__<name>/…` and the legacy `repo/…`. Returning the REMAINDER (rather
    than a bool) is what lets callers test `stories/<id>/` without caring which.
    """
    if path.startswith("repo/"):
        return path[len("repo/"):]
    if path.startswith("repos/"):
        _owner_name, _, tail = path[len("repos/"):].partition("/")
        if _owner_name and tail:
            return tail
    return None


def _is_plan_path(path: str, story_id: str | None = None) -> bool:
    """True if `path` looks like a plan file under a supported convention.

    Canonical: repo/stories/<id>/plans/cycle-NN-<slug>.md (also accepts the interim
    plan_<name> filename in that dir). Plus the legacy repo/docs/plans/ + docs/plans/.
    Any `.md` in a story's plans/ subdir counts (except INDEX.md) — cycle-NN and
    plan_ alike."""
    if not path.endswith(".md") or path.endswith("INDEX.md"):
        return False
    rest = _strip_repo_root(path)
    if rest and rest.startswith("stories/") and "/plans/" in rest:
        return True
    if (rest and rest.startswith("docs/plans/")) or path.startswith("docs/plans/"):
        return True
    return False


def _plan_path_rank(path: str, story_id: str | None) -> int:
    """TIEBREAKER ONLY — the scan sorts by recency (mtime) first, so this only
    decides between two plan files with identical mtimes (rare). The canonical
    convention (what the planning skill instructs Claude to write and what the
    validate-plan hook enforces) is
    `repo/stories/<story_id>/plans/cycle-NN-<slug>.md`, so a plan under THIS story's
    plans/ subdir ranks highest; another story's subdir (shared repo), the prior
    `repo/docs/plans/` form, and the legacy workspace-root `docs/plans/` form rank
    below it.

    Recency is the PRIMARY key (see _find_latest_plan_path), so on a hook-miss
    fallback the most-recently-written plan wins regardless of convention — this
    rank only breaks exact-mtime ties and keeps the canonical convention on top.
    """
    rest = _strip_repo_root(path)
    if rest and rest.startswith("stories/") and "/plans/" in rest:
        if story_id and rest.startswith(f"stories/{story_id}/"):
            return 3  # this story's canonical subdir
        return 2      # another story's subdir (shared repo)
    if rest and rest.startswith("docs/plans/"):
        return 1      # prior convention, still accepted
    return 0          # docs/plans/ at workspace root (legacy)


async def _find_latest_plan_path(
    instance_id: str, story_id: str,
) -> str | None:
    """Find the most recently modified plan file in the workspace.

    Recognizes three conventions in order of preference:
      1. NEW (migration 059+): repo/stories/<story_id>/plans/plan_*.md
      2. PRIOR: repo/docs/plans/*.md
      3. LEGACY: docs/plans/*.md (workspace root, pre-Phase-4)

    Among matching files, prefers the newer convention, then ties broken
    by mtime (most-recently-modified wins).
    """
    try:
        result = await gateway_admin.call(
            "shca.workspace.listFiles",
            {"agentId": instance_id, "storyId": story_id},
            timeout=30,
        )
    except GatewayError as exc:
        _log.warning("listFiles failed: %s", exc)
        return None
    entries = result.get("entries") or []
    plan_files = [
        e for e in entries
        if e.get("type") == "file" and _is_plan_path(e.get("path", ""), story_id)
    ]
    if not plan_files:
        return None
    # Sort by RECENCY first (the most-recently-written plan is where Claude just
    # wrote — that's what "approve" must ingest), convention rank only as a tiebreaker.
    def _rank(e):
        return (e.get("mtimeMs", 0), _plan_path_rank(e.get("path", ""), story_id))
    plan_files.sort(key=_rank, reverse=True)
    return plan_files[0]["path"]


async def _read_plan_file(
    instance_id: str, story_id: str, path: str,
) -> tuple[str, bool]:
    """Returns (content, truncated). Raises on RPC failure."""
    result = await gateway_admin.call(
        "shca.workspace.readFile",
        {"agentId": instance_id, "storyId": story_id, "path": path},
        timeout=30,
    )
    # Strip null bytes that the gateway's readFile RPC can inject as
    # binary-padding artifacts from the underlying filesystem/CLI.
    content = (result.get("content") or "").replace('\x00', '')
    return content, bool(result.get("truncated"))


async def _push_plan_to_repo(
    *, instance_id: str, story_id: str, plan_path: str, version: int,
) -> tuple[bool, str | None]:
    """Commit + push the plan file to the user's branch.

    gitCommit runs `git add` inside <workspace>/stories/<sid>/repo/, so
    the path must be repo-relative. Plans are stored at
    `repo/stories/<story_id>/plans/cycle-NN-<slug>.md` from the workspace, which
    becomes `stories/<story_id>/plans/cycle-NN-<slug>.md` from the repo's
    perspective (the leading `repo/` is stripped below).

    Returns (pushed, error_message). Errors are non-fatal — plan
    approval still succeeds.
    """
    # The clone dir is not always literally `repo/`: a multi-repo workspace uses
    # `repos/<owner>__<name>/`. Hardcoding `repo/` meant a multi-repo story's approved
    # plan was silently never committed or pushed ("not in repo/ — skipping push"),
    # even though it was written exactly where the agent was told to write it.
    #
    # Read from the RECORDED plan path rather than re-derived from the grants: deriving
    # it meant granting a repository that sorts earlier alphabetically silently moved
    # where plans were expected, so this check would start rejecting the very path the
    # agent had been told to write to. `validate_plan` resolves the root the same way,
    # which is what keeps the gate and this commit in agreement.
    from app.services.story_plan_root import resolve_plan_root
    from app.database import get_pool as _get_pool
    clone_root = await resolve_plan_root(_get_pool(), story_id)
    prefix = f"{clone_root}/"
    if not plan_path.startswith(prefix):
        msg = (
            f"plan file at workspace root ({plan_path}); not in {prefix} — "
            f"skipping push. Move plans to {prefix}stories/<story_id>/plans/ "
            f"to enable."
        )
        _log.info("plan_approval: %s", msg)
        return False, msg

    repo_relative_path = plan_path[len(prefix):]

    # Refresh the GitHub installation token inline so the credential
    # helper holds a fresh password right before push. The 50-min loop
    # may not have run yet for this story (e.g. first dispatch).
    from app.services.token_refresh import refresh_story_token
    refreshed, refresh_err = await refresh_story_token(story_id)
    if not refreshed:
        _log.info(
            "plan_approval: pre-push token refresh skipped/failed "
            "(story=%s): %s — attempting push with existing credentials",
            story_id, refresh_err,
        )

    try:
        result = await gateway_admin.call(
            "shca.workspace.gitCommit",
            {
                "agentId": instance_id,
                "storyId": story_id,
                "paths": [repo_relative_path],
                "message": f"plan: v{version} — {repo_relative_path}",
                "push": True,
            },
            timeout=60,
        )
    except GatewayError as exc:
        _log.warning(
            "plan_approval: gitCommit RPC failed (story=%s plan=%s): %s",
            story_id, plan_path, exc,
        )
        return False, f"gitCommit RPC failed: {exc}"
    except Exception as exc:
        _log.exception("plan_approval: gitCommit raised (story=%s)", story_id)
        return False, f"unexpected: {exc}"

    if result.get("nothing_to_commit"):
        _log.info(
            "plan_approval: nothing to commit (story=%s plan=%s) — already pushed",
            story_id, plan_path,
        )
        return True, None
    if result.get("pushed"):
        _log.info(
            "plan_approval: plan pushed (story=%s plan=%s commit=%s)",
            story_id, plan_path, (result.get("commit") or "")[:12],
        )
        return True, None
    push_err = result.get("push_error") or "unknown push failure"
    _log.warning(
        "plan_approval: gitCommit returned pushed=false (story=%s plan=%s): %s "
        "[full result keys=%s]",
        story_id, plan_path, push_err, sorted(result.keys()),
    )
    return False, push_err


async def _copy_plan_to_other_write_repos(
    *, instance_id: str, story_id: str, plan_path: str, plan_md: str,
) -> None:
    """Write the SAME plan content (and scope.md, if present) into every OTHER
    write repo's own tree, at the same relative path.

    Mirrors the VS Code extension's local-mode `copyPlanToOtherWriteRepos`
    (2026-08-20): the assistant's own instruction-following for "copy the plan
    into every write repo" proved unreliable across sessions (473eb1ee vs
    c22f2501, same primary repo, two different plan locations) — asking it to
    remember a SECOND copy step would reintroduce exactly that risk. This makes
    it true by construction instead.

    Best-effort: a copy failure is logged but never fails plan approval. Copies
    are left UNCOMMITTED here, same as the extension does — they land in each
    repo's own commit naturally when that repo's work is later committed and
    pushed at the end of a normal execution cycle.
    """
    from app.services.story_plan_root import resolve_plan_root  # noqa: PLC0415
    from app.services.story_workspace_layout import (  # noqa: PLC0415
        resolve_workspace_clones, write_clones,
    )
    pool = get_pool()
    clone_root = await resolve_plan_root(pool, story_id)
    if not plan_path.startswith(f"{clone_root}/"):
        return  # not a canonical in-repo plan path — nothing to mirror

    clones = await resolve_workspace_clones(pool, story_id)
    other_subdirs = [c["subdir"] for c in write_clones(clones) if c["subdir"] != clone_root]
    if not other_subdirs:
        return  # single-repo story — nothing to mirror into

    repo_relative_plan_path = plan_path[len(clone_root) + 1:]
    scope_rel = f"stories/{story_id}/scope.md"
    scope_md: str | None = None
    try:
        result = await gateway_admin.call(
            "shca.workspace.readFile",
            {"agentId": instance_id, "storyId": story_id, "path": f"{clone_root}/{scope_rel}"},
            timeout=30,
        )
        scope_md = (result.get("content") or "").replace("\x00", "") or None
    except Exception:
        pass  # scope.md may not exist yet (created optionally) — fine to skip

    for subdir in other_subdirs:
        for rel, content in ((repo_relative_plan_path, plan_md), (scope_rel, scope_md)):
            if content is None:
                continue
            try:
                await gateway_admin.call(
                    "shca.workspace.writeFile",
                    {
                        "agentId": instance_id, "storyId": story_id,
                        "path": f"{subdir}/{rel}", "content": content,
                    },
                    timeout=30,
                )
            except Exception as exc:
                _log.warning(
                    "plan_approval: couldn't mirror %s into %s (story=%s): %s",
                    rel, subdir, story_id, exc,
                )


async def approve_plan(
    *,
    goal_id: str,
    story_id: str,
    approved_by_uid: str,
    plan_path: str | None = None,
) -> dict:
    """Run the full approval flow. Returns:
      { ok, dag_task_id, plan_version_id, version, task_count,
        plan_path, plan_pushed, push_error? }
    On failure (parse error / read failure): {ok: False, error}
    """
    # Kill switch — a plan approval ingests a DAG and dispatches the first tasks,
    # which would bypass a system/goal freeze just like autonomous dispatch does.
    # Shared with the web "Approve plan" button and the Slack ctrl_approve_plan
    # button, so one check covers both surfaces. dispatch_block fails closed.
    from app.services import execution_control  # noqa: PLC0415
    _frozen = await execution_control.frozen_response(goal_id)
    if _frozen is not None:
        _log.info(
            "approve_plan refused — execution paused, goal=%s story=%s uid=%s",
            goal_id, story_id, approved_by_uid,
        )
        return _frozen

    pool = get_pool()
    story = await pool.fetchrow(
        """SELECT shca_instance_id, current_phase, current_claude_session_id,
                  current_plan_id, current_plan_path, active_thread_id
             FROM stories WHERE id = $1 AND goal_id = $2""",
        story_id, goal_id,
    )
    if not story:
        return {"ok": False, "error": "story not found"}
    instance_id = (story.get("shca_instance_id") or "").strip()
    claude_session_id = (story.get("current_claude_session_id") or "").strip()
    if not instance_id:
        return {"ok": False, "error": "story has no SHCA instance — workspace not provisioned"}

    # If the session id wasn't captured at spawn time (no code path writes
    # it yet — known gap), discover it by listing ~/.claude/projects/<...>/.
    # Persist the discovered id so subsequent dispatches don't need to look
    # it up again.
    if not claude_session_id:
        try:
            find_result = await gateway_admin.call(
                "shca.workspace.findClaudeSession",
                {"agentId": instance_id, "storyId": story_id},
                timeout=15,
            )
        except GatewayError as exc:
            return {
                "ok": False,
                "error": (
                    "story has no current_claude_session_id and findClaudeSession "
                    f"RPC failed: {exc}"
                ),
            }
        discovered = (find_result.get("sessionId") or "").strip()
        if not discovered:
            return {
                "ok": False,
                "error": (
                    "no Claude session found on disk for this story — Claude may "
                    "not have been spawned yet, or the workspace path is wrong"
                ),
            }
        await pool.execute(
            "UPDATE stories SET current_claude_session_id = $1, updated_at = NOW() "
            "WHERE id = $2",
            discovered, story_id,
        )
        _log.info(
            "plan_approval: recovered claude_session_id=%s for story=%s (was empty in DB)",
            discovered, story_id,
        )
        claude_session_id = discovered

    # 1. Locate the plan file. Priority:
    #    (a) plan_path passed explicitly by caller (UI button, replan flow)
    #    (b) stories.current_plan_path — set by /internal/shca/validate-plan
    #        on every successful plan-file write Claude makes. Authoritative
    #        because it's written at the moment Claude touches the file;
    #        no mtime races, no chat-history parsing.
    #    (c) Workspace mtime scan — legacy fallback for stories whose
    #        plans predate migration 060 / the validator-hook tracking.
    if plan_path is None:
        plan_path = (story.get("current_plan_path") or "").strip() or None
    if plan_path is None:
        plan_path = await _find_latest_plan_path(instance_id, story_id)
    if not plan_path:
        return {
            "ok": False,
            "error": (
                f"no plan file found in the workspace. Expected one of:\n"
                f"  - repo/stories/{story_id}/plans/cycle-NN-<slug>.md (current convention)\n"
                f"  - repo/docs/plans/<name>.md (prior convention)\n"
                f"  - docs/plans/<name>.md (legacy)\n"
                f"Tell Claude to write the plan and try Approve again."
            ),
        }
    _log.info(
        "plan_approval: using plan_path=%s (in_repo=%s) story=%s",
        plan_path, plan_path.startswith("repo/"), story_id,
    )

    # 2. Read the plan content. If the recorded path 404s (e.g. validator
    # stored a path that doesn't resolve), fall back to a workspace mtime
    # scan to recover gracefully — same fallback we use when current_plan_path
    # is empty.
    try:
        plan_md, truncated = await _read_plan_file(instance_id, story_id, plan_path)
    except GatewayError as exc:
        if "file not found" in str(exc).lower():
            _log.warning(
                "plan_approval: recorded plan_path=%s 404'd (story=%s) — "
                "falling back to workspace scan",
                plan_path, story_id,
            )
            fallback = await _find_latest_plan_path(instance_id, story_id)
            if fallback and fallback != plan_path:
                _log.info(
                    "plan_approval: workspace scan recovered plan_path=%s "
                    "(story=%s, original=%s)",
                    fallback, story_id, plan_path,
                )
                plan_path = fallback
                try:
                    plan_md, truncated = await _read_plan_file(
                        instance_id, story_id, plan_path,
                    )
                except GatewayError as exc2:
                    return {
                        "ok": False,
                        "error": (
                            f"failed to read plan file: {exc2} "
                            f"(also tried fallback path={fallback})"
                        ),
                    }
            else:
                return {"ok": False, "error": f"failed to read plan file: {exc}"}
        else:
            return {"ok": False, "error": f"failed to read plan file: {exc}"}
    if truncated:
        _log.warning(
            "plan_approval: plan file %s was truncated at the 1MB cap — "
            "DAG parsing may miss tail content",
            plan_path,
        )

    # 3. Run DAG ingestion (validates + inserts tasks).
    try:
        ingest_result = await ingest_approved_plan(
            goal_id=goal_id,
            story_id=story_id,
            plan_path=plan_path,
            plan_md=plan_md,
            claude_session_id=claude_session_id,
            approved_by_uid=approved_by_uid,
        )
    except DagParseError as exc:
        return {"ok": False, "error": f"plan DAG invalid: {exc}", "plan_path": plan_path}
    except Exception as exc:
        _log.exception("plan_approval ingest crashed (story=%s): %s", story_id, exc)
        return {"ok": False, "error": f"DAG ingestion failed: {exc}"}

    # 4. Commit + push the plan file (best-effort).
    plan_pushed, push_error = await _push_plan_to_repo(
        instance_id=instance_id, story_id=story_id,
        plan_path=plan_path, version=ingest_result["version"],
    )

    # 4b. Mirror the plan (+ scope.md) into every OTHER write repo (best-effort,
    # multi-repo only — no-op for a single-repo story). Runs on EVERY approval,
    # so a replan's cycle-02/03/… gets mirrored exactly like the first plan.
    try:
        await _copy_plan_to_other_write_repos(
            instance_id=instance_id, story_id=story_id,
            plan_path=plan_path, plan_md=plan_md,
        )
    except Exception:
        _log.exception(
            "plan_approval: mirroring plan to other write repos failed (story=%s)",
            story_id,
        )

    # 5. Trigger task controller wake so first task dispatches (trigger is async — await it).
    try:
        from app.task_controller import trigger as tc_trigger
        await tc_trigger()
    except Exception:
        # Non-fatal — controller will pick it up on next tick anyway.
        pass

    # 6. Broadcast a live notification to any open SHCA chat tab so the user
    #    can see execution started without having to type or refresh.
    try:
        _thread_id = story.get("active_thread_id")
        if _thread_id:
            _thread_row = await pool.fetchrow(
                "SELECT agent_id FROM threads WHERE id = $1", _thread_id,
            )
            _agent_id = _thread_row["agent_id"] if _thread_row else None
            if _agent_id:
                from app.openclaw import session_manager
                await session_manager.broadcast_to_thread(
                    goal_id, _agent_id, _thread_id,
                    {
                        "type": "plan_approved_notify",
                        "task_count": ingest_result["task_count"],
                        "story_id": story_id,
                    },
                )
                _log.info(
                    "plan_approval: broadcast plan_approved_notify "
                    "(story=%s thread=%s tasks=%d)",
                    story_id, _thread_id, ingest_result["task_count"],
                )
    except Exception:
        _log.debug(
            "plan_approval: plan_approved_notify broadcast failed (non-fatal, story=%s)",
            story_id, exc_info=True,
        )

    return {
        "ok": True,
        "dag_task_id": ingest_result["dag_task_id"],
        "plan_version_id": ingest_result["plan_version_id"],
        "version": ingest_result["version"],
        "task_count": ingest_result["task_count"],
        "task_ids": ingest_result["task_ids"],
        "plan_path": plan_path,
        "plan_pushed": plan_pushed,
        "push_error": push_error,
    }
