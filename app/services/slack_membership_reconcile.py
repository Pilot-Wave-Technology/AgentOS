"""Periodically make every Slack channel's membership match its thread.

SLACK CANNOT FORBID AN INVITE. Any human already in a private channel
may add another human, and there is no setting or scope that prevents
it. So "this channel is visible only to the people in its thread" is a
property that is true when the channel is created and decays from
there -- quietly, because nobody is notified and nothing logs it.

An access rule that is only enforced at creation is not an access rule.
This loop is the enforcement.

WHY A LOOP RATHER THAN AN EVENT. `member_joined_channel` exists and
would be faster, but it is a Slack event: it arrives only while the
integration is healthy, the signature verifies and the handler does not
throw. A membership guarantee cannot be contingent on all three. The
event is a reasonable future optimisation ON TOP of this; it is not a
replacement for it.

Registered in app/services/scheduler/registry.py, not only in main.py --
an environment with a scheduler beside the panel sets
AGENTOS_SCHEDULER_LOOPS=0, and a loop registered only in main.py then
runs nowhere at all.
"""

from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)

INTERVAL_SECONDS = 900          # 15 minutes
_BATCH = 200                    # channels per pass, oldest-checked first


async def reconcile_once(pool) -> dict:
    """One pass. Returns a summary for the log and for tests."""
    from app.channels.slack.membership import sync_channel_members

    # FINISHED STORIES FIRST, so the pass does not spend its budget
    # reconciling the membership of channels it is about to archive.
    from app.services.slack_retire import sweep as _retire_sweep
    retired = await _retire_sweep(pool)

    rows = await pool.fetch(
        """
        SELECT b.thread_id, b.goal_id
          FROM platform_channel_bindings b
          JOIN platform_integrations pi ON pi.id = b.integration_id
          JOIN goal_platform_integrations gpi
                 ON gpi.integration_id = b.integration_id
                AND gpi.goal_id        = b.goal_id
         WHERE b.archived_at IS NULL
           AND b.thread_id IS NOT NULL
           AND pi.platform = 'slack'
           AND pi.status   = 'active'
           AND gpi.enabled = true
         ORDER BY b.updated_at ASC
         LIMIT $1
        """, _BATCH)

    checked = invited = removed = 0
    for r in rows:
        # A CHANNEL THAT SHOULD NEVER HAVE EXISTED. `is_slack_excluded`
        # stops the goal review council, security council and agent mesh
        # threads getting a channel -- but only from the moment it was
        # added. The three already standing on feature were made before
        # it, and an exclusion that only applies to new threads leaves
        # exactly the channels the rule exists to prevent.
        #
        # So the pass retires them. This is also the one path that can:
        # nothing else revisits a binding after it is made, and Slack
        # has no delete outside Enterprise Grid, so "retired" means
        # archived and the name stays reserved for ever -- which is why
        # it is driven by the same predicate as creation rather than by
        # a list of names somebody maintains.
        try:
            from app.channels.slack.thread_channels import (
                archive_channel_for_thread, is_slack_excluded)
            if await is_slack_excluded(pool, r["goal_id"], r["thread_id"]):
                if await archive_channel_for_thread(pool, r["thread_id"]):
                    retired += 1
                    _log.info("Slack reconcile: retired the channel for "
                              "excluded thread %s", (r["thread_id"] or "")[:12])
                continue
        except Exception as exc:                               # noqa: BLE001
            _log.warning("Slack reconcile: could not check exclusion for "
                         "%s: %s", (r["thread_id"] or "")[:12], exc)

        try:
            out = await sync_channel_members(pool, r["thread_id"], kick=True)
        except Exception as exc:                               # noqa: BLE001
            # One bad channel must not end the pass; the next one may be
            # the one with a stranger in it.
            _log.warning("Slack reconcile: thread %s failed: %s",
                         (r["thread_id"] or "")[:12], exc)
            continue
        checked += 1
        invited += len(out["invited"])
        removed += len(out["kicked"])

    if invited or removed or retired:
        _log.info("Slack reconcile: %d channel(s), %d invited, %d removed, "
                  "%d retired", checked, invited, removed, retired)
    return {"checked": checked, "invited": invited, "removed": removed,
            "retired": retired}


async def run_loop() -> None:
    from app.database import get_pool

    _log.info("Slack membership reconcile loop started (every %ds)",
              INTERVAL_SECONDS)
    while True:
        try:
            await reconcile_once(get_pool())
        except Exception as exc:                               # noqa: BLE001
            _log.warning("Slack reconcile pass failed: %s", exc, exc_info=True)
        await asyncio.sleep(INTERVAL_SECONDS)
