"""Turning Slack on for a goal that already has threads.

SLACK IS OFF BY DEFAULT AND SWITCHED ON WHENEVER SOMEBODY WANTS IT --
that part already worked: `goal_platform_integrations.enabled` is per
goal, every lookup joins on it, and flipping it stops or starts the
relay immediately.

WHAT DID NOT WORK IS TURNING IT ON LATE. Provisioning created the
channels that existed at that moment, which under the old model was
the goal channel plus one per active member. Under one channel per
thread, a goal switched on after twenty stories needs twenty channels
for the threads already there -- and nothing made them, so every one
of those conversations stayed invisible while new ones appeared.

Two properties this has to have, both learned the hard way elsewhere
in this rework:

  IDEMPOTENT, because somebody will toggle it twice, and because a
  half-finished run must be safe to repeat.

  SERIALISED, through the same create path everything else uses.
  `conversations.create` is Tier-2, roughly twenty a minute, and
  twenty threads at once is exactly the burst that finds that out.
"""

from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)

# Between creates. Tier-2 is ~20/min; this keeps a large goal inside it
# without making a small one feel slow.
_GAP_SECONDS = 3.0


async def live_threads(pool, goal_id: str) -> list[dict]:
    """Threads in this goal that should have a channel and have none.

    TWO FILTERS, AND THE SECOND ONE IS NOT OBVIOUS.

    Terminal stories are skipped: a channel for a story finished months
    ago would be created only to be archived, and would spend the rate
    limit the live ones need.

    AND A THREAD MUST ACTUALLY BE IN USE. The first version of this took
    every live thread in the goal, which sounded right and was not: the
    old model gave each member one persistent (user, goal) thread and
    accumulated ad-hoc ones beside it, and those are still there,
    dormant. Measured on feature before anybody ran it, goal S1VSD would
    have created **129 channels, none of them story threads**. That is
    129 Tier-2 creates, a workspace full of junk, and every one of those
    names reserved for ever, because Slack never releases an archived
    channel's name.

    So: a thread with a live story, or one of the goal's four named GO
    threads. That is exactly the set the system creates channels for
    going forward -- `ensure_story_thread` and `ensure_go_story_thread`
    are the only callers of `ensure_channel_for_thread` -- and a
    back-fill that covered more than the live path would be inventing
    channels nothing will ever post to.
    """
    rows = await pool.fetch(
        """
        SELECT t.id, t.title, t.agent_id, t.story_id, t.created_by,
               u.actor_id
          FROM threads t
          LEFT JOIN stories s ON s.id = t.story_id
          LEFT JOIN users u ON u.uid = t.created_by
          JOIN goals g ON g.id = t.goal_id
         WHERE t.goal_id = $1
           -- A DELETED GOAL GETS NOTHING. Goal delete is soft
           -- (`goals.status='deleted'`) and does not cascade to threads,
           -- so a deleted goal keeps every thread it ever had. On feature
           -- that is 145 of 147 goals, and the largest of them, S1VSD,
           -- holds 129 -- so without this the one thing that could make
           -- the back-fill catastrophic is a goal nobody uses any more.
           -- (`enable_channel` would not run for one, but defence in
           -- depth is cheap and this is the ninth goal-active guard in
           -- the codebase for exactly this reason.)
           AND g.status = 'active'
           AND t.left_goal_at IS NULL
           AND (
                 (t.story_id IS NOT NULL AND s.id IS NOT NULL
                  AND s.state NOT IN ('done', 'reviewed', 'cancelled',
                                      'rejected'))
                 -- The GO's own thread only. The orchestrator (agent
                 -- mesh), goal review council and security council
                 -- threads are read-only machine logs that nobody
                 -- answers in Slack; `is_slack_excluded` refuses them
                 -- anyway, and listing them here would just mean a
                 -- back-fill that counts work it will not do.
                 OR t.id = g.go_thread_id
               )
           AND NOT EXISTS (
                 SELECT 1 FROM platform_channel_bindings b
                  WHERE b.thread_id = t.id AND b.archived_at IS NULL)
         ORDER BY t.created_at
        """, goal_id)
    return [dict(r) for r in rows]


async def backfill_goal(pool, goal_id: str, *, limit: int = 100) -> dict:
    """Give every live thread in this goal a channel. Returns a summary.

    Never raises: it runs on the edge of the request that enabled Slack,
    and a workspace being unreachable must not make the toggle fail --
    the goal IS enabled, and the reconcile pass will find what this
    missed.
    """
    made, failed, skipped = 0, 0, 0
    try:
        threads = await live_threads(pool, goal_id)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack backfill: could not list threads for %s: %s",
                     goal_id, exc)
        return {"created": 0, "failed": 0, "skipped": 0, "error": str(exc)}

    if len(threads) > limit:
        skipped = len(threads) - limit
        threads = threads[:limit]

    from app.channels.slack.thread_channels import ensure_channel_for_thread

    for n, t in enumerate(threads):
        try:
            got = await ensure_channel_for_thread(
                pool, goal_id=goal_id, thread_id=t["id"],
                title=t["title"] or "", agent_id=t["agent_id"] or "",
                actor_id=str(t["actor_id"]) if t["actor_id"] else None,
                story_id=t["story_id"])
            if got:
                made += 1
            else:
                failed += 1
        except Exception as exc:                               # noqa: BLE001
            failed += 1
            _log.warning("Slack backfill: thread %s failed: %s",
                         str(t["id"])[:12], exc)
        if n + 1 < len(threads):
            await asyncio.sleep(_GAP_SECONDS)

    if made or failed:
        _log.info("Slack backfill goal=%s: %d created, %d failed, %d deferred",
                  goal_id, made, failed, skipped)
    return {"created": made, "failed": failed, "skipped": skipped, "error": ""}
