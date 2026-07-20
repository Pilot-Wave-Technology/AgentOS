"""Deliver a Security Council report to every active goal that references the branch.

Mirrors the Goal Review Council's two-surface delivery: (1) persist a visible
message to each goal thread attributed to the "Security Council" so humans see
it, and (2) wake the GO with an inbound message so it can react. The difference
is fan-out: one branch review reaches all goals that have the branch as a resource.
"""
from __future__ import annotations

import logging

from app.database import get_pool
from app.services.execution_control import dispatch_block

log = logging.getLogger(__name__)

SECURITY_SENDER = "Security Council"


async def deliver_report(run: dict, report_markdown: str) -> int:
    """Deliver the convener's report to every active goal referencing the branch.

    Returns the number of goals delivered to.
    """
    pool = get_pool()
    # Deliver to every NON-DELETED goal (goals.status='active') that references the
    # branch — NOT goals.state='active'. `state` is the lifecycle
    # (planning/active/paused/completed); a goal in planning still has live branch
    # resources and should get the report. `status` is the soft-delete flag — the
    # correct "is this goal real" test.
    goals = await pool.fetch(
        "SELECT DISTINCT g.id, g.go_thread_id FROM goals g "
        "JOIN goal_resources r ON r.goal_id = g.id "
        "WHERE g.status = 'active' AND r.status = 'active' "
        "AND r.kind = 'branch' AND lower(r.branch_name) = lower($2) "
        "AND lower(r.clone_url) = lower($1)",
        run["clone_url"], run["branch_name"],
    )
    if not goals:
        log.info("security: run=%s — no live goals reference branch %s; report not delivered",
                 run["id"], run["branch_name"])
        return 0

    title = f"Security Council — {run['repo_owner']}/{run['repo_name']} @ {run['branch_name']}"
    delivered = 0
    for g in goals:
        if not g["go_thread_id"]:
            continue
        # Kill switch — a paused goal gets nothing this run. The scan already ran
        # (it's shared across goals on this branch), so this only withholds
        # delivery; the goal picks up the next run's findings once resumed.
        blk = await dispatch_block(g["id"])
        if blk:
            log.info("security: run=%s goal=%s paused (scope=%s) — withholding delivery",
                     run["id"], g["id"], blk.scope)
            continue
        # Persist a review card to each affected goal's ORCHESTRATOR channel
        # (humans see it there — kept out of the GO's main goal thread).
        try:
            from app.services.orchestrator_delivery import persist_review_to_orchestrator
            await persist_review_to_orchestrator(
                g["id"], "security", title, report_markdown, SECURITY_SENDER,
            )
        except Exception as exc:
            log.warning("security: orchestrator persist failed for %s: %s", g["id"], exc)
        # Wake each affected GO with the FULL review (marked) so the report is in
        # the GO's context to act on. The [REVIEW:*] marker triggers the
        # review_action_planning skill; the GO replies on its main goal thread.
        # The wake is NOT rendered in the goal-thread UI, so the main thread stays
        # decluttered — the human-facing copy is the orchestrator card above.
        wake = f"[REVIEW:security] {title}\n\n{report_markdown}"
        try:
            from app.services.go_events import _send_system_message_to_go
            # mirror_to_mesh=False: already persisted as a card above — mirroring
            # here too would duplicate it, unformatted, into Agent Mesh.
            await _send_system_message_to_go(g["id"], wake, wake_up=True, mirror_to_mesh=False)
        except Exception as exc:
            log.debug("security: GO wake-up failed for %s: %s", g["id"], exc)
        delivered += 1
    log.info("security: run=%s delivered report to %d goal(s)", run["id"], delivered)
    return delivered
