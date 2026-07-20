"""Pre-integration gate handler.

Builds a local merge preview (merge target INTO feature in the agent workspace),
generates the merge report, and archives it to GCS. If conflicts are detected,
creates a conflict-resolution coder task and marks the gate blocked.

Gateway dependency: this handler calls the 'workspace.mergePreview' admin RPC,
which must be implemented in agent_os_openclaw. The RPC runs the git operations
(fetch, merge --no-commit, conflict detection) in the story's existing workspace
on the OpenClaw VM — necessary because the integration-tester will run in that
same workspace and needs to see the merged state.

Expected RPC contract:
  request:  workspace.mergePreview {
                session_key, source_branch, target_branch,
                write_report_to: 'docs/integration/merge_report.md'
            }
  response: {
                success: bool,
                conflict_files: list[str],  # empty on success
                changed_files: list[str],   # files the feature branch adds/changes
                source_sha: str,
                target_sha: str,
                preview_merge_sha: str      # only on success
            }
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from app.database import get_pool
from app.ledger.journal import append_event
from app.storage import upload_resource_file

_log = logging.getLogger(__name__)

from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID
_REPORT_WORKSPACE_PATH = "docs/integration/merge_report.md"


async def handle_pre_integration_gate(gate: dict, pool) -> None:
    """Execute the pre-integration gate: build merge preview, generate report.

    On success: marks gate done, unblocking the integration-tester phase task.
    On conflict: marks gate blocked, spawns conflict-resolution coder task.
    On fatal error: marks gate failed.
    """
    task_id = gate["id"]
    story_id = gate.get("story_id")
    goal_id = gate.get("goal_id")
    payload = gate.get("gate_payload") or {}
    if isinstance(payload, str):
        _log.debug("pre_integration: gate_payload was a string — parsing JSON (gate=%s)", gate["id"])
        payload = json.loads(payload)

    repo_resource_id = payload.get("repo_goal_resource_id")
    source_branch = payload.get("source_branch", "")
    target_branch = payload.get("target_branch", "")

    # Validate repo_resource_id is present. goal_resources.id is a TEXT primary
    # key using 12-char truncated UUIDs (str(uuid4())[:12]), not full UUID format —
    # so we only check for presence, not UUID format validity.
    if not repo_resource_id:
        _log.error(
            "pre_integration: gate %s has missing repo_goal_resource_id — failing gate",
            task_id,
        )
        await pool.execute("UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1", task_id)
        from app.services.merge_orchestrator.dispatch import on_gate_failed  # noqa: PLC0415
        await on_gate_failed(gate, "gate_payload.repo_goal_resource_id is missing")
        return

    _log.info(
        "pre_integration: gate=%s story=%s source=%s target=%s",
        task_id, story_id, source_branch, target_branch,
    )

    # Resolve the agent session key for the workspace.mergePreview RPC.
    # The session key identifies the workspace where the feature branch is checked out.
    session_key = await _resolve_session_key(pool, story_id, goal_id)
    if not session_key:
        _log.error("pre_integration: could not resolve session key for story %s — failing gate", story_id)
        await pool.execute("UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1", task_id)
        from app.services.merge_orchestrator.dispatch import on_gate_failed  # noqa: PLC0415
        await on_gate_failed(gate, "Could not resolve an OpenClaw session key for this story — no active workspace found.")
        return

    # Create the integration_attempts row before calling the gateway
    # so we have an attempt_id for the GCS path.
    attempt_id = str(uuid.uuid4())
    attempt_number = await _next_attempt_number(pool, story_id)
    branch_resource_id = await _resolve_branch_goal_resource_id(pool, story_id)

    await pool.execute(
        """INSERT INTO integration_attempts
               (id, actor_type, actor_id, goal_resource_id,
                branch_goal_resource_id,
                source_branch_name, target_branch_name, attempt_number, mechanism,
                gcs_report_uri, created_at)
           VALUES ($1, 'story', $2, $3, $4, $5, $6, $7, 'git_merge', '', NOW())""",
        attempt_id, story_id, repo_resource_id,
        branch_resource_id,
        source_branch, target_branch, attempt_number,
    )
    _log.info("pre_integration: created integration_attempts row %s (attempt %d)", attempt_id, attempt_number)

    # Record a resource_activity_signal so concurrent stories targeting the same
    # branch know this story is integrating.
    await _upsert_activity_signal(pool, repo_resource_id, target_branch, story_id, task_id, attempt_id)

    # Get a short-lived GitHub token and the repo URL so workspace.mergePreview
    # can add/update the origin remote and authenticate the fetch.
    # The SHCA workspace is a local git repo with no origin configured by default —
    # the workspace.mergePreview RPC must set the remote before it can fetch.
    github_token, repo_url = await _get_github_token_and_url_for_repo(pool, repo_resource_id)

    # Call the gateway workspace.mergePreview RPC.
    # This RPC runs on the OpenClaw VM in the story's existing workspace (the same
    # shared workspace all claude-code-phase tasks use — coder already cloned the repo).
    preview = await _call_merge_preview_rpc(session_key, source_branch, target_branch, github_token, repo_url)

    if preview is None:
        _log.error("pre_integration: mergePreview RPC failed for gate %s — failing gate", task_id)
        await _update_attempt(pool, attempt_id, result="aborted")
        await pool.execute("UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1", task_id)
        from app.services.merge_orchestrator.dispatch import on_gate_failed  # noqa: PLC0415
        await on_gate_failed(gate, "workspace.mergePreview RPC failed — the OpenClaw extension may not be deployed or the workspace is unavailable. Check Cloud Run logs for details.")
        return

    _log.info(
        "pre_integration: mergePreview result gate=%s success=%s "
        "source_sha=%s target_sha=%s changed_files=%d conflict_files=%d",
        task_id, preview.get("success"),
        preview.get("source_sha", "")[:12], preview.get("target_sha", "")[:12],
        len(preview.get("changed_files", [])), len(preview.get("conflict_files", [])),
    )

    if not preview.get("success"):
        conflict_files = preview.get("conflict_files", [])
        _log.warning(
            "pre_integration: merge preview conflict in gate %s — files=%s",
            task_id, conflict_files,
        )
        await _handle_conflict(
            pool=pool,
            task_id=task_id,
            attempt_id=attempt_id,
            story_id=story_id,
            goal_id=goal_id,
            source_branch=source_branch,
            target_branch=target_branch,
            conflict_files=conflict_files,
            target_sha=preview.get("target_sha", ""),
        )
        return

    # Success: generate and archive the merge report.
    report_md = _build_merge_report(
        story_id=story_id,
        source_branch=source_branch,
        target_branch=target_branch,
        source_sha=preview.get("source_sha", ""),
        target_sha=preview.get("target_sha", ""),
        changed_files=preview.get("changed_files", []),
        attempt_number=attempt_number,
    )

    gcs_uri = await _archive_report(story_id, attempt_id, report_md)
    # result=None here because the attempt is still in-progress (integration-tester runs next).
    # result='success' is set by _finalize_success in the integrate handler after the human approves.
    await _update_attempt(pool, attempt_id, result=None, gcs_report_uri=gcs_uri)
    _log.info("pre_integration: merge report archived to %s", gcs_uri)

    # Mark gate done and emit ledger event in a single connection so append_event
    # gets the asyncpg.Connection it requires for advisory locking + hash chaining.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET state = 'done', updated_at = NOW() WHERE id = $1",
            task_id,
        )
        await conn.execute(
            "UPDATE stories SET current_phase = 'integrating', updated_at = NOW() WHERE id = $1",
            story_id,
        )
        await append_event(
            conn,
            actor_id=_SYSTEM_ACTOR_ID, actor_type="system",
            event_type="integration.preview_complete",
            payload={
                "gate_task_id": task_id,
                "story_id": story_id,
                "attempt_id": attempt_id,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "gcs_report_uri": gcs_uri,
            },
            context_goal_id=goal_id,
        )

    # Wake the task controller so the integration-tester phase task is picked up
    # immediately rather than waiting for the next poll tick.
    try:
        from app.task_controller import trigger as _tc_trigger  # noqa: PLC0415
        await _tc_trigger()
    except Exception:
        _log.debug("pre_integration: could not trigger task controller — next tick will pick up")

    _log.info("pre_integration: gate %s done — integration-tester now ready for story %s", task_id, story_id)


async def _resolve_session_key(pool, story_id: str, goal_id: str) -> str | None:
    """Return the OpenClaw session key for the story's existing Claude workspace.

    story_claude_execution_contexts (migration 046) has zero writers — it is
    deprecated in place. The authoritative pointer is stories.shca_instance_id,
    written by operator_provisioning.py at worker spawn time.
    """
    row = await pool.fetchrow(
        "SELECT shca_instance_id FROM stories WHERE id = $1",
        story_id,
    )
    if row and row["shca_instance_id"]:
        agent_id = row["shca_instance_id"]
        _log.debug("pre_integration: resolved session key story=%s agent_id=%s", story_id, agent_id)
        return f"agent:{agent_id}:{goal_id}"

    _log.warning(
        "pre_integration: stories.shca_instance_id is NULL for story=%s — no active workspace",
        story_id,
    )
    return None


async def _next_attempt_number(pool, story_id: str) -> int:
    existing = await pool.fetchval(
        "SELECT COALESCE(MAX(attempt_number), 0) FROM integration_attempts WHERE actor_id = $1",
        story_id,
    )
    return (existing or 0) + 1


async def _resolve_branch_goal_resource_id(pool, story_id: str) -> str | None:
    """Return the goal_resources.id of the branch this story produces.

    story_branches (migration 039) is deprecated — zero rows in dev+feature.
    The canonical assignment is story_resources WHERE relation='produces' joined
    to goal_resources WHERE kind='branch'.
    """
    row = await pool.fetchrow(
        """SELECT gr.id
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND sr.relation IN ('produces', 'needs')
              AND gr.kind = 'branch'
            ORDER BY
                CASE WHEN sr.relation = 'produces' THEN 0 ELSE 1 END,
                gr.created_at ASC
            LIMIT 1""",
        story_id,
    )
    if not row:
        _log.debug(
            "pre_integration: no produces-branch story_resource found for story=%s "
            "— integration_attempts.branch_goal_resource_id will be NULL",
            story_id,
        )
        return None
    return str(row["id"])


async def _upsert_activity_signal(
    pool, repo_resource_id: str, target_branch: str, story_id: str, task_id: str, attempt_id: str
) -> None:
    """Write an in_progress signal for this story's integration target."""
    assigned_to = await pool.fetchval("SELECT assigned_to FROM stories WHERE id = $1", story_id)
    metadata = json.dumps({"dag_task_id": task_id, "integration_attempt_id": attempt_id})
    await pool.execute(
        """INSERT INTO resource_activity_signals
               (repo_goal_resource_id, target_branch_name, actor_type, actor_id,
                activity_type, assigned_to, status, metadata, created_at)
           VALUES ($1, $2, 'story', $3, 'integrating', $4, 'in_progress', $5::JSONB, NOW())""",
        repo_resource_id, target_branch, story_id, assigned_to, metadata,
    )
    _log.info(
        "pre_integration: activity signal written for repo=%s branch=%s story=%s",
        repo_resource_id, target_branch, story_id,
    )


async def _get_github_token_and_url_for_repo(pool, repo_resource_id: str) -> tuple[str | None, str]:
    """Return (github_token, repo_url) for the repo resource.

    repo_url is the HTTPS GitHub URL needed to configure the origin remote in the
    SHCA workspace (which has no origin by default). Token and URL are resolved
    independently — a token failure does not suppress the URL.
    """
    # ── resolve URL ──────────────────────────────────────────────────────────
    repo_url = ""
    full_repo_name = ""
    try:
        row = await pool.fetchrow(
            "SELECT repo_owner, repo_name, clone_url FROM goal_resources WHERE id = $1",
            repo_resource_id,
        )
        if not row or not row["repo_name"]:
            _log.warning(
                "pre_integration: cannot resolve repo_name for resource %s — no url or token",
                repo_resource_id,
            )
            return None, ""
        repo_name = row["repo_name"]
        # Prefer the stored clone_url, then build from repo_owner+repo_name.
        repo_url = (
            row["clone_url"]
            or (f"https://github.com/{row['repo_owner']}/{repo_name}" if row["repo_owner"] else "")
        )
        full_repo_name = f"{row['repo_owner']}/{repo_name}" if row["repo_owner"] else repo_name
        _log.info(
            "pre_integration: resolved repo_url=%s full_repo_name=%s for resource=%s",
            repo_url, full_repo_name, repo_resource_id,
        )
    except Exception:
        _log.exception("pre_integration: failed to resolve repo url for resource %s", repo_resource_id)
        return None, ""

    # ── resolve token (independent — failure does not suppress repo_url) ─────
    # generate_installation_token expects bare repo names (not owner/repo).
    # It returns {"token": "ghs_...", "expires_at": "..."} — extract the string.
    token: str | None = None
    try:
        from app.github_tokens import generate_installation_token  # noqa: PLC0415
        token_data = await generate_installation_token([repo_name])
        token = token_data.get("token") if isinstance(token_data, dict) else str(token_data)
        _log.info("pre_integration: GitHub token obtained for repo %s", repo_name)
    except Exception:
        _log.exception(
            "pre_integration: failed to get GitHub token for repo %s — will proceed without token",
            repo_name,
        )

    return token, repo_url


async def _call_merge_preview_rpc(
    session_key: str,
    source_branch: str,
    target_branch: str,
    github_token: str | None = None,
    repo_url: str = "",
) -> dict | None:
    """Call the OpenClaw workspace.mergePreview admin RPC.

    workspace.mergePreview runs git operations directly on the OpenClaw VM in the
    story's shared workspace. The SHCA workspace is a local git repo with no origin
    remote by default — repo_url is passed so the extension can add/update origin
    before fetching the target branch.

    github_token authenticates the fetch (Claude session credential helper is only
    active during agent turns, not during server-side RPC execution).
    """
    try:
        from app.openclaw import gateway_admin  # noqa: PLC0415
        params = {
            "session_key": session_key,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "write_report_to": _REPORT_WORKSPACE_PATH,
            "story_id": story_id or "",
        }
        if github_token:
            params["github_token"] = github_token
        if repo_url:
            params["repo_url"] = repo_url
        _log.info(
            "pre_integration: calling workspace.mergePreview session=%s source=%s target=%s repo_url=%s token_present=%s",
            session_key, source_branch, target_branch, repo_url, bool(github_token),
        )
        result = await gateway_admin.call("workspace.mergePreview", params)
        _log.info(
            "pre_integration: mergePreview RPC returned success=%s conflict_files=%s changed_files=%s",
            result.get("success"), result.get("conflict_files"), len(result.get("changed_files", [])),
        )
        return result
    except Exception:
        _log.exception("pre_integration: workspace.mergePreview RPC failed")
        return None


def _build_merge_report(
    *,
    story_id: str,
    source_branch: str,
    target_branch: str,
    source_sha: str,
    target_sha: str,
    changed_files: list[str],
    attempt_number: int,
) -> str:
    """Generate the merge_report.md that the integration-tester and human reviewer read."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    files_block = "\n".join(f"- `{f}`" for f in changed_files) if changed_files else "_No files changed_"
    return f"""# Merge Report

**Story:** {story_id}
**Generated:** {ts}
**Attempt:** {attempt_number}

## Branches

| | Branch | SHA |
|---|---|---|
| Source (feature) | `{source_branch}` | `{source_sha or "unknown"}` |
| Target (trunk) | `{target_branch}` | `{target_sha or "unknown"}` |

## Merge preview result

**Status:** Preview merge succeeded — no conflicts detected.

The workspace is on the merge preview state (target merged into feature locally).
The integration-tester should verify the merged codebase against the original
acceptance criteria. Read this file for context on what was merged.

## Files changed by this story

{files_block}

## Next step

The integration-tester phase will run next. After it passes, the integrate gate
will present this report alongside the net diff for human review.

---
_This report is a durable audit artifact. The canonical copy is archived in GCS
(`integration_attempts.gcs_report_uri`). The workspace copy is transient._
"""


async def _archive_report(story_id: str, attempt_id: str, report_md: str) -> str:
    """Upload the merge report to the shared resource bucket and return the GCS URI.

    Path convention: stories/{story_id}/integration/{attempt_id}/merge_report.md
    Uses story_id as resource_id so the path fits upload_resource_file's
    {resource_id}/{filename} pattern. See DECISION_TRACEABILITY.md.
    """
    filename = f"integration/{attempt_id}/merge_report.md"
    uri = await upload_resource_file(
        resource_id=f"stories/{story_id}",
        filename=filename,
        data=report_md.encode("utf-8"),
        content_type="text/markdown",
    )
    return uri


async def _update_attempt(pool, attempt_id: str, *, result: str | None, gcs_report_uri: str = "") -> None:
    if gcs_report_uri:
        await pool.execute(
            "UPDATE integration_attempts SET gcs_report_uri = $1, updated_at = NOW() WHERE id = $2",
            gcs_report_uri, attempt_id,
        )
    if result is not None:
        await pool.execute(
            "UPDATE integration_attempts SET result = $1, resolved_at = NOW(), updated_at = NOW() WHERE id = $2",
            result, attempt_id,
        )


_DEFAULT_MAX_RETRIES = 3


async def _handle_conflict(
    *, pool, task_id: str, attempt_id: str, story_id: str, goal_id: str,
    source_branch: str, target_branch: str, conflict_files: list[str], target_sha: str,
) -> None:
    """Record the conflict, check retry ceiling, and spawn a conflict-resolution coder task.

    Mechanism:
      1. Record the conflicted attempt row.
      2. Check if retries are exhausted → block story and notify SHCA.
      3. Insert a claude-code-phase coder task with a [MERGE-CONFLICT] brief.
      4. Transition the G1 gate from in_progress → pending with depends_on=[resolver_id].
         When the resolver completes, _resolve_dependents in sentinel_parser resets G1
         to ready automatically so the gate re-runs without any additional hooks.
    """
    _log.warning(
        "pre_integration: handling merge conflict gate=%s story=%s "
        "source=%s target=%s target_sha=%s conflict_file_count=%d files=%s",
        task_id, story_id, source_branch, target_branch,
        target_sha[:12] if target_sha else "", len(conflict_files), conflict_files,
    )

    conflict_details = {
        "conflict_files": conflict_files,
        "trunk_sha_at_attempt": target_sha,
        "error_message": "Merge preview detected conflicts — conflict-resolution coder spawned.",
    }
    await pool.execute(
        """UPDATE integration_attempts
           SET result = 'conflicted', conflict_details = $1::JSONB, resolved_at = NOW()
           WHERE id = $2""",
        json.dumps(conflict_details), attempt_id,
    )

    # Check retry ceiling before spawning another resolver.
    # Count prior conflicted/push_rejected attempts for this story.
    failed_attempts = await pool.fetchval(
        """SELECT COUNT(*) FROM integration_attempts
            WHERE actor_id = $1
              AND result IN ('conflicted', 'push_rejected')""",
        story_id,
    )
    max_retries = await _resolve_max_retries(pool, story_id)
    _log.info(
        "pre_integration: conflict retry check story=%s failed_attempts=%d max_retries=%d",
        story_id, failed_attempts, max_retries,
    )

    if failed_attempts > max_retries:
        _log.error(
            "pre_integration: retry ceiling reached for story=%s (%d > %d) — blocking story",
            story_id, failed_attempts, max_retries,
        )
        await pool.execute(
            """UPDATE stories
               SET state = 'blocked',
                   blocked_reason = $1,
                   current_phase = 'resolving_conflicts',
                   updated_at = NOW()
               WHERE id = $2""",
            (
                f"Integration with trunk failed after {failed_attempts} attempts. "
                f"Last conflict files: {', '.join(conflict_files[:5])}. "
                f"Reply to the SHCA thread with guidance to retry, or increase "
                f"goal_resources.metadata.max_integration_retries."
            ),
            story_id,
        )
        await pool.execute(
            "UPDATE tasks SET state = 'blocked', updated_at = NOW() WHERE id = $1",
            task_id,
        )
        from app.services.merge_orchestrator.dispatch import on_gate_failed  # noqa: PLC0415
        await on_gate_failed(
            {"id": task_id, "story_id": story_id, "goal_id": goal_id, "gate_kind": "pre-integration"},
            (
                f"Merge conflict retry ceiling reached ({failed_attempts} failed attempts, "
                f"max {max_retries}). Conflicting files: {', '.join(conflict_files[:10])}. "
                f"Reply to the SHCA thread with resolution guidance to unblock, or update "
                f"`goal_resources.metadata.max_integration_retries` to allow more retries."
            ),
        )
        return

    # Look up the parent DAG task to get claude_session_id for the resolver.
    dag_task = await pool.fetchrow(
        "SELECT id, claude_session_id FROM tasks WHERE id = (SELECT parent_task_id FROM tasks WHERE id = $1)",
        task_id,
    )
    if not dag_task:
        # Fallback: find the session id directly from a sibling coder phase task.
        dag_task = await pool.fetchrow(
            """SELECT parent_task_id AS id, claude_session_id
                 FROM tasks
                WHERE story_id = $1
                  AND executor = 'claude-code-phase'
                  AND state = 'done'
                ORDER BY created_at ASC LIMIT 1""",
            story_id,
        )
    claude_session_id = dag_task["claude_session_id"] if dag_task else None
    dag_task_id = dag_task["id"] if dag_task else None

    # Build the [MERGE-CONFLICT] brief for the resolver coder task.
    files_block = "\n".join(f"- `{f}`" for f in conflict_files) if conflict_files else "_(unknown — check workspace)_"
    resolver_description = f"""\
Merge conflict resolution required.

## Conflict context

A pre-integration merge preview of `{source_branch}` into `{target_branch}` detected
conflicts. The trunk was at SHA `{target_sha[:12] if target_sha else "unknown"}` at the
time of the preview. The integration gate (G1) is paused and will re-run automatically
once this task is complete.

**Conflicting files:**

{files_block}

## Your task

1. Fetch the latest trunk: `git fetch origin {target_branch}`
2. Rebase onto it: `git rebase origin/{target_branch}`
   - If rebase fails with conflicts, resolve each conflicting file. Remove all merge
     markers (`<<<<<<<`, `=======`, `>>>>>>>`). Stage resolved files with `git add`.
     Then `git rebase --continue`.
   - Alternative: `git merge origin/{target_branch}` if rebase is inappropriate.
3. Verify the workspace compiles / basic checks pass.
4. `git status` must show a clean working tree before completing.

Do NOT push — the gate handles push after re-running the preview.

## Acceptance criterion

All conflict markers removed, workspace on a clean rebased commit with
`git status` showing nothing to commit, working tree clean.
"""

    # Insert the resolver as a sibling coder task in the same DAG.
    resolver_id = f"{dag_task_id}-R{attempt_id[:6]}" if dag_task_id else str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO tasks (
               id, goal_id, story_id, parent_task_id,
               title, description,
               executor, phase,
               claude_session_id,
               state,
               acceptance_criteria,
               depends_on,
               created_by, created_at, updated_at
           ) VALUES (
               $1, $2, $3, $4,
               $5, $6,
               'claude-code-phase', 'coder',
               $7,
               'ready',
               'Clean working tree, no merge markers, rebased onto target',
               '{}'::TEXT[],
               $8, NOW(), NOW()
           )""",
        resolver_id, goal_id, story_id, dag_task_id,
        f"[MERGE-CONFLICT] Resolve conflicts: {source_branch} ↔ {target_branch}",
        resolver_description,
        claude_session_id,
        _SYSTEM_ACTOR_ID,
    )
    _log.info(
        "pre_integration: inserted resolver task %s for gate=%s story=%s",
        resolver_id, task_id, story_id,
    )

    # Transition G1 gate from in_progress → pending with resolver as dependency.
    # When the resolver completes, sentinel_parser._resolve_dependents will flip
    # this gate back to ready and the task_controller will re-run it.
    await pool.execute(
        """UPDATE tasks
               SET state = 'pending',
                   depends_on = ARRAY[$1]::TEXT[],
                   updated_at = NOW()
             WHERE id = $2""",
        resolver_id, task_id,
    )
    await pool.execute(
        "UPDATE stories SET current_phase = 'resolving_conflicts', updated_at = NOW() WHERE id = $1",
        story_id,
    )
    _log.warning(
        "pre_integration: gate=%s set to pending on resolver=%s; "
        "will auto-requeue when resolver completes (story=%s files=%s)",
        task_id, resolver_id, story_id, conflict_files,
    )

    # Notify SHCA so the human knows conflicts were found and a resolver is running.
    from app.services.sentinel_parser import write_task_progress_card  # noqa: PLC0415
    await write_task_progress_card(
        goal_id=goal_id,
        story_id=story_id,
        task_id=task_id,
        phase="pre-integration",
        kind="conflict_detected",
        text=(
            f"[integration-gate] Merge conflict detected in **pre-integration** gate.\n\n"
            f"**Source:** `{source_branch}` → `{target_branch}`\n\n"
            f"**Conflicting files ({len(conflict_files)}):**\n"
            + "\n".join(f"- `{f}`" for f in conflict_files[:10])
            + ("\n_…and more_" if len(conflict_files) > 10 else "")
            + f"\n\n"
            f"A conflict-resolution coder task has been spawned (task `{resolver_id}`). "
            f"The integration gate will re-run automatically once it completes.\n\n"
            f"Attempt **{failed_attempts}** of **{max_retries + 1}** allowed."
        ),
        sentinel_kind="CONFLICT",
    )


async def _resolve_max_retries(pool, story_id: str) -> int:
    """Return max_integration_retries from the goal's repo resource metadata (default 3)."""
    row = await pool.fetchrow(
        """SELECT gr.metadata
             FROM goal_resources gr
             JOIN stories s ON s.goal_id = gr.goal_id
            WHERE s.id = $1
              AND gr.kind = 'repo'
            ORDER BY gr.created_at ASC LIMIT 1""",
        story_id,
    )
    if not row:
        return _DEFAULT_MAX_RETRIES
    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return int(metadata.get("max_integration_retries", _DEFAULT_MAX_RETRIES))
