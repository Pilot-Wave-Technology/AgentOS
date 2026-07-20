"""Route GitEvent objects from the receiver to the correct orchestrator handler.

The dispatcher knows nothing about providers — it receives provider-agnostic
GitEvent objects from the per-provider handlers and looks up which story/gate
the event belongs to via the integration_attempts table.
"""
from __future__ import annotations

import logging

from app.database import get_pool
from .github_handler import GitEvent

_log = logging.getLogger(__name__)


async def dispatch(event: GitEvent) -> None:
    """Route the event to the correct handler based on mr_id + repo."""
    _log.info(
        "event_dispatcher: dispatching event kind=%s mr=%s repo=%s",
        event.kind, event.mr_id, event.repo_full_name,
    )

    pool = get_pool()

    # Look up the integration_attempts row by mr_id and repo match.
    # The mr_id is stored on integration_attempts when the MR is created.
    attempt = await pool.fetchrow(
        """SELECT ia.*, t.id AS gate_task_id
             FROM integration_attempts ia
             JOIN story_branches sb ON sb.id = ia.story_branch_id
             LEFT JOIN tasks t ON t.story_id = ia.actor_id
                              AND t.executor = 'system-gate'
                              AND t.gate_kind = 'integrate'
                              AND t.state = 'awaiting_approval'
            WHERE ia.mr_id = $1 AND ia.result IS NULL
            LIMIT 1""",
        event.mr_id,
    )

    if not attempt:
        _log.warning(
            "event_dispatcher: no active integration_attempts row for mr_id=%s — event ignored",
            event.mr_id,
        )
        return

    gate_task_id = attempt["gate_task_id"]
    if not gate_task_id:
        # LEFT JOIN found no awaiting_approval integrate gate for this attempt.
        # The MR event arrived after the gate already advanced (e.g. handle_integrate_approval ran first).
        _log.warning(
            "event_dispatcher: no awaiting_approval integrate gate for attempt %s — event ignored",
            attempt["id"],
        )
        return

    if event.kind == "mr_merged":
        # Webhook confirms the MR was merged externally (e.g. human approved via GitHub UI).
        # The integrate gate is still awaiting_approval — advance it.
        _log.info("event_dispatcher: mr_merged for gate %s — finalising", gate_task_id)
        from app.services.merge_orchestrator.handlers.integrate import _finalize_success  # noqa: PLC0415
        await _finalize_success(
            pool=pool,
            task_id=gate_task_id,
            story_id=attempt["actor_id"],
            goal_id=await pool.fetchval("SELECT goal_id FROM tasks WHERE id = $1", gate_task_id),
            attempt_id=str(attempt["id"]),
            repo_resource_id=str(attempt["goal_resource_id"]),
            target_branch=attempt["target_branch_name"],
            landing_mode="mr",
            new_sha="",  # not available from webhook; already merged
            approved_by_uid="webhook",
        )

    elif event.kind == "mr_closed":
        _log.info("event_dispatcher: mr_closed for gate %s — marking attempt aborted", gate_task_id)
        await pool.execute(
            "UPDATE integration_attempts SET result = 'aborted', resolved_at = NOW() WHERE id = $1",
            attempt["id"],
        )

    else:
        _log.debug("event_dispatcher: event kind=%s not actionable for gate %s", event.kind, gate_task_id)
