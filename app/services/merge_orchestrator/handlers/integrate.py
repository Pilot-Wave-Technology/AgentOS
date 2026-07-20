"""Integrate gate handler — the single human approval gate.

Transitions the gate to awaiting_approval and writes a review card with:
  - Net diff (feature vs. target) — requires agent_os_openclaw gitDiff extension
  - Merge report (from integration_attempts.gcs_report_uri)
  - Allowed landing modes (direct_push omitted if branch protection requires a PR)

On approval (called from the tasks approval router):
  handle_integrate_approval() executes the chosen landing mode and marks the gate done.

See documentation/story-integration/SYSTEM_GATE.md and FLOW.md for the full design.
"""
from __future__ import annotations

import json
import logging
import uuid

from app.database import get_pool
from app.ledger.journal import append_event
from app.services.git_providers import get_provider

_log = logging.getLogger(__name__)
from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID


async def handle_integrate_gate(gate: dict, pool) -> None:
    """Transition the integrate gate to awaiting_approval and write a review card.

    The gate waits in this state until the human approves via the tasks UI.
    The approval route calls handle_integrate_approval() to execute the push/MR.
    """
    task_id = gate["id"]
    story_id = gate.get("story_id")
    goal_id = gate.get("goal_id")
    payload = gate.get("gate_payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    repo_resource_id = payload.get("repo_goal_resource_id")
    source_branch = payload.get("source_branch", "")
    target_branch = payload.get("target_branch", "")
    provider_name = payload.get("provider", "")

    _log.info(
        "integrate: gate=%s story=%s source=%s target=%s provider=%s",
        task_id, story_id, source_branch, target_branch, provider_name,
    )

    # Determine which landing modes are available based on branch protection.
    allowed_landing_modes = await _check_landing_modes(repo_resource_id, target_branch, pool)
    _log.info("integrate: allowed_landing_modes=%s for gate %s", allowed_landing_modes, task_id)

    # Fetch the latest merge report URI for the approval card.
    latest_attempt = await pool.fetchrow(
        """SELECT id, gcs_report_uri FROM integration_attempts
           WHERE actor_id = $1 AND result IS NULL
           ORDER BY attempt_number DESC LIMIT 1""",
        story_id,
    )
    gcs_report_uri = latest_attempt["gcs_report_uri"] if latest_attempt else ""
    attempt_id = str(latest_attempt["id"]) if latest_attempt else ""

    # Compute net diff for the review card.
    # Requires agent_os_openclaw gitDiff baseRef/headRef extension — see CODE_INTEGRATION.md §6.
    diff_summary = await _fetch_net_diff(pool, goal_id, story_id, source_branch, target_branch)

    # Write the approval card.
    await _write_approval_card(
        pool=pool,
        task_id=task_id,
        story_id=story_id,
        goal_id=goal_id,
        source_branch=source_branch,
        target_branch=target_branch,
        allowed_landing_modes=allowed_landing_modes,
        gcs_report_uri=gcs_report_uri,
        diff_summary=diff_summary,
    )

    # Persist allowed_landing_modes back into gate_payload so handle_integrate_approval
    # can read it without re-querying branch protection.
    updated_payload = {**payload, "allowed_landing_modes": allowed_landing_modes, "attempt_id": attempt_id}
    await pool.execute(
        """UPDATE tasks SET state = 'awaiting_approval', gate_payload = $1::JSONB, updated_at = NOW()
           WHERE id = $2""",
        json.dumps(updated_payload), task_id,
    )
    await pool.execute(
        "UPDATE stories SET current_phase = 'awaiting_mr_approval', updated_at = NOW() WHERE id = $1",
        story_id,
    )

    _log.info("integrate: gate %s now awaiting human approval (story=%s)", task_id, story_id)


async def handle_integrate_approval(
    *,
    task_id: str,
    landing_mode: str,
    approved_by_uid: str,
) -> None:
    """Execute the approved integration: push/MR based on landing_mode.

    Called by the tasks approval router when the human approves the integrate gate.
    landing_mode: 'direct_push' | 'mr'
    """
    pool = get_pool()

    gate = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if not gate:
        _log.error("integrate.approval: gate task %s not found", task_id)
        return

    payload = gate["gate_payload"] or {}
    if isinstance(payload, str):
        _log.debug("integrate.approval: gate_payload was a string — parsing JSON (gate=%s)", task_id)
        payload = json.loads(payload)

    story_id = gate["story_id"]
    goal_id = gate["goal_id"]
    repo_resource_id = payload.get("repo_goal_resource_id")
    source_branch = payload.get("source_branch", "")
    target_branch = payload.get("target_branch", "")
    attempt_id = payload.get("attempt_id", "")
    allowed = payload.get("allowed_landing_modes", ["mr"])

    _log.info(
        "integrate.approval: task=%s story=%s mode=%s approved_by=%s "
        "source=%s target=%s repo_resource=%s attempt=%s allowed=%s",
        task_id, story_id, landing_mode, approved_by_uid,
        source_branch, target_branch, repo_resource_id, attempt_id, allowed,
    )

    if landing_mode not in allowed:
        _log.error(
            "integrate.approval: landing_mode=%r not in allowed=%s for gate %s — rejecting",
            landing_mode, allowed, task_id,
        )
        return

    # Load the repo resource to get provider credentials.
    repo_resource = await pool.fetchrow("SELECT * FROM goal_resources WHERE id = $1", repo_resource_id)
    if not repo_resource:
        _log.error("integrate.approval: repo resource %s not found — failing gate", repo_resource_id)
        await pool.execute("UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1", task_id)
        from app.services.merge_orchestrator.dispatch import on_gate_failed  # noqa: PLC0415
        await on_gate_failed(dict(gate), f"Repo goal resource not found: {repo_resource_id!r}. The resource may have been deleted.")
        return

    provider = await get_provider(dict(repo_resource))
    merge_result = None

    if landing_mode == "direct_push":
        _log.info("integrate.approval: executing direct push %s → %s (gate=%s)", source_branch, target_branch, task_id)
        merge_result = await provider.direct_push_merge(source_branch, target_branch)
        _log.info(
            "integrate.approval: direct push result success=%s sha=%s (gate=%s)",
            getattr(merge_result, "success", None), getattr(merge_result, "new_trunk_sha", ""), task_id,
        )

    elif landing_mode == "mr":
        title = f"feat: merge {source_branch} into {target_branch}"
        description = await _build_mr_description(pool, story_id, attempt_id)
        _log.info("integrate.approval: creating MR source=%s target=%s (gate=%s)", source_branch, target_branch, task_id)
        mr_result = await provider.create_mr(source_branch, target_branch, title, description)
        _log.info(
            "integrate.approval: MR created mr_id=%s url=%s (gate=%s)",
            mr_result.mr_id, mr_result.mr_url, task_id,
        )

        # Record MR details on the attempt before merging.
        await pool.execute(
            """UPDATE integration_attempts
               SET mr_id = $1, mr_url = $2, mr_provider = $3
               WHERE id = $4""",
            mr_result.mr_id, mr_result.mr_url, payload.get("provider", ""), attempt_id,
        )
        _log.debug("integrate.approval: MR details recorded on attempt %s", attempt_id)

        _log.info("integrate.approval: merging MR %s (gate=%s)", mr_result.mr_id, task_id)
        merge_result = await provider.merge_mr(mr_result.mr_id)
        _log.info(
            "integrate.approval: merge_mr result success=%s sha=%s (gate=%s)",
            getattr(merge_result, "success", None), getattr(merge_result, "new_trunk_sha", ""), task_id,
        )
        if not merge_result.success:
            _log.warning(
                "integrate.approval: MR merge failed — conflict or protection; "
                "gate stays awaiting_approval (gate=%s mr_id=%s)",
                task_id, mr_result.mr_id,
            )
            # The gate remains awaiting_approval for the human to retry.
            return

    if merge_result and merge_result.success:
        _log.info(
            "integrate.approval: merge succeeded — finalizing gate=%s sha=%s",
            task_id, getattr(merge_result, "new_trunk_sha", ""),
        )
        await _finalize_success(
            pool=pool,
            task_id=task_id,
            story_id=story_id,
            goal_id=goal_id,
            attempt_id=attempt_id,
            repo_resource_id=repo_resource_id,
            target_branch=target_branch,
            landing_mode=landing_mode,
            new_sha=merge_result.new_trunk_sha,
            approved_by_uid=approved_by_uid,
        )
    else:
        _log.error(
            "integrate.approval: merge operation produced no success result "
            "(merge_result=%r landing_mode=%r gate=%s) — marking failed",
            merge_result, landing_mode, task_id,
        )
        await pool.execute("UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1", task_id)
        from app.services.merge_orchestrator.dispatch import on_gate_failed  # noqa: PLC0415
        await on_gate_failed(dict(gate), f"Merge operation failed (landing_mode={landing_mode!r}). The branch may have been protected or a conflict appeared after approval.")


async def _check_landing_modes(repo_resource_id: str, target_branch: str, pool) -> list[str]:
    """Query branch protection and return the allowed landing modes."""
    repo_resource = await pool.fetchrow("SELECT * FROM goal_resources WHERE id = $1", repo_resource_id)
    if not repo_resource:
        _log.warning("integrate: repo resource %s not found — defaulting to MR only", repo_resource_id)
        return ["mr"]
    try:
        provider = await get_provider(dict(repo_resource))
        protection = await provider.get_branch_protection(target_branch)
        if protection.pr_review_required:
            _log.info("integrate: branch protection on %s requires PR — direct_push disabled", target_branch)
            return ["mr"]
        return ["direct_push", "mr"]
    except Exception:
        _log.exception("integrate: branch protection query failed for %s — defaulting to MR only", target_branch)
        return ["mr"]


async def _fetch_net_diff(
    pool, goal_id: str, story_id: str, source_branch: str, target_branch: str
) -> str:
    """Fetch the net diff (target..feature) via the gateway gitDiff extension.

    Requires agent_os_openclaw gitDiff baseRef/headRef extension (CODE_INTEGRATION.md §6).
    Falls back to a placeholder if the extension is not yet deployed.
    """
    _log.info(
        "integrate: fetching net diff story=%s source=%s target=%s",
        story_id, source_branch, target_branch,
    )
    try:
        session_key = await _resolve_session_key_for_story(pool, story_id, goal_id)
        if not session_key:
            _log.warning(
                "integrate: no session key for story=%s — diff skipped (workspace may not exist)",
                story_id,
            )
            return "(session key unavailable — diff not computed)"

        from app.openclaw import gateway_admin  # noqa: PLC0415
        result = await gateway_admin.call("workspace.gitDiff", {
            "session_key": session_key,
            "baseRef": target_branch,
            "headRef": source_branch,
        })
        diff = result.get("diff", "") or ""
        _log.info("integrate: gitDiff returned %d chars for story=%s", len(diff), story_id)
        return diff or "(diff empty)"
    except Exception:
        _log.exception(
            "integrate: gitDiff RPC failed story=%s source=%s target=%s "
            "(agent_os_openclaw workspace-ops extension may not be deployed)",
            story_id, source_branch, target_branch,
        )
        return "(net diff unavailable — review the merge report for context)"


async def _resolve_session_key_for_story(pool, story_id: str, goal_id: str) -> str | None:
    """Return the OpenClaw session key for the story's existing Claude workspace.

    story_claude_execution_contexts (migration 046) has zero writers — deprecated in place.
    The authoritative pointer is stories.shca_instance_id.
    """
    row = await pool.fetchrow("SELECT shca_instance_id FROM stories WHERE id = $1", story_id)
    if row and row["shca_instance_id"]:
        key = f"agent:{row['shca_instance_id']}:{goal_id}"
        _log.debug("integrate: resolved session key story=%s key=%s", story_id, key)
        return key
    _log.error(
        "integrate: could not resolve session key for story=%s — "
        "stories.shca_instance_id is NULL (no active workspace)",
        story_id,
    )
    return None


async def _build_mr_description(pool, story_id: str, attempt_id: str) -> str:
    story = await pool.fetchrow("SELECT title, description FROM stories WHERE id = $1", story_id)
    title = story["title"] if story else story_id
    desc = story["description"] if story else ""
    attempt = await pool.fetchrow(
        "SELECT gcs_report_uri, target_branch_name FROM integration_attempts WHERE id = $1", attempt_id
    )
    report_uri = attempt["gcs_report_uri"] if attempt else ""
    return (
        f"## {title}\n\n{desc}\n\n"
        f"---\n"
        f"*Raised by AgentOS merge orchestrator.*\n"
        f"Merge report: `{report_uri}`"
    )


async def _write_approval_card(
    *, pool, task_id: str, story_id: str, goal_id: str,
    source_branch: str, target_branch: str,
    allowed_landing_modes: list[str], gcs_report_uri: str, diff_summary: str,
) -> None:
    """Write the approval card to the SHCA thread and persist it on the task row.

    Uses write_task_progress_card which:
      - Writes a message into the story's active thread (visible in the UI)
      - Broadcasts via WebSocket so the human sees it in real time
      - Forwards to the SHCA's gateway session so the LLM sees it too
    """
    modes_text = " / ".join(m.replace("_", " ").title() for m in allowed_landing_modes)

    # Truncate diff to keep card readable — human can pull the full diff from the GCS report.
    diff_preview = diff_summary[:3000]
    if len(diff_summary) > 3000:
        diff_preview += f"\n\n_…diff truncated ({len(diff_summary)} chars total). Full diff in merge report._"

    card_text = (
        f"[integration-gate] **Integrate gate** — `{source_branch}` → `{target_branch}`\n\n"
        f"Integration testing passed. Review the diff and merge report below, "
        f"then approve via the Tasks UI.\n\n"
        f"**Landing modes available:** {modes_text}\n\n"
        f"**Merge report (GCS):** `{gcs_report_uri or 'not yet archived'}`\n\n"
        f"**Net diff (feature vs. target):**\n```\n{diff_preview}\n```"
    )

    _log.info("integrate: writing approval card for gate %s story %s", task_id, story_id)

    # Persist summary on task.description so the Tasks UI approval widget shows it.
    await pool.execute(
        "UPDATE tasks SET description = $1, updated_at = NOW() WHERE id = $2",
        card_text, task_id,
    )

    # Post to the story's SHCA thread so the human and SHCA LLM both see it.
    from app.services.sentinel_parser import write_task_progress_card  # noqa: PLC0415
    await write_task_progress_card(
        goal_id=goal_id,
        story_id=story_id,
        task_id=task_id,
        phase="integrate",
        kind="awaiting_approval",
        text=card_text,
        sentinel_kind="AWAITING_APPROVAL",
    )


async def _finalize_success(
    *, pool, task_id: str, story_id: str, goal_id: str, attempt_id: str,
    repo_resource_id: str, target_branch: str, landing_mode: str,
    new_sha: str, approved_by_uid: str,
) -> None:
    """Record success, clean up signal, and mark gate done.

    All writes and the append_event call share one connection because append_event
    requires asyncpg.Connection (uses advisory locking + hash chaining, not a pool).
    """
    _log.info(
        "integrate: finalizing success gate=%s story=%s attempt=%s target=%s mode=%s sha=%s approved_by=%s",
        task_id, story_id, attempt_id, target_branch, landing_mode, new_sha, approved_by_uid,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE integration_attempts
               SET result = 'success', resolved_at = NOW(), updated_at = NOW()
               WHERE id = $1""",
            attempt_id,
        )
        _log.debug("integrate: integration_attempt %s marked success", attempt_id)
        # story_branches (migration 039) is deprecated — zero rows; no update needed.

        # Complete the resource_activity_signal — notifies any waiting stories.
        await conn.execute(
            """UPDATE resource_activity_signals
               SET status = 'completed', completed_at = NOW()
               WHERE actor_id = $1 AND status = 'in_progress'""",
            story_id,
        )
        _log.debug("integrate: resource_activity_signal completed for story=%s", story_id)

        # Gate done — deployment phase (if present) becomes ready via _resolve_dependencies.
        await conn.execute(
            "UPDATE tasks SET state = 'done', updated_at = NOW() WHERE id = $1",
            task_id,
        )
        await conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_next', updated_at = NOW() WHERE id = $1",
            story_id,
        )

        await append_event(
            conn,
            actor_id=_SYSTEM_ACTOR_ID, actor_type="system",
            event_type="integration.complete",
            payload={
                "gate_task_id": task_id,
                "story_id": story_id,
                "attempt_id": attempt_id,
                "landing_mode": landing_mode,
                "new_sha": new_sha,
                "approved_by": approved_by_uid,
            },
            context_goal_id=goal_id,
        )

    _log.info(
        "integrate: gate %s done — story %s merged to %s via %s (sha=%s)",
        task_id, story_id, target_branch, landing_mode, new_sha,
    )
