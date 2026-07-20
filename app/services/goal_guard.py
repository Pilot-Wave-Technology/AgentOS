"""Single source of truth for "is this goal dead?" — used to block creating GO or
Assistant (SHCA) sessions, gateway agents, or instances for a goal that has been
soft-deleted (or hard-removed).

Rule: once a goal's status is 'deleted' (or the row is gone), nothing new should be
provisioned or warmed for its GO or Assistants. This complements the deleted-goal
session reaper (which evicts *existing* sessions) by stopping *new* creation at the
source, so a stale trigger / headless warmer / queued message can't resurrect a
dead goal's agents.

Only 'deleted' (and missing) block creation — active / planning / paused goals are
unaffected.
"""
from __future__ import annotations

import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

async def goal_is_dead(goal_id: str, pool=None) -> bool:
    """True if the goal is NOT active — i.e. its `status` column is anything other
    than 'active' (deleted / cancelled / completed / …) or the row is missing. Only
    status='active' goals may get GO/Assistant sessions, gateway agents, or instances.
    (Goal *state* — planning/active/paused — is a SEPARATE column and is not consulted
    here; a live planning goal still has status='active'.)

    Fail-safe: on a DB error return False so a transient blip never blocks a live goal."""
    if not goal_id:
        return False
    try:
        pool = pool or get_pool()
        status = await pool.fetchval("SELECT status FROM goals WHERE id = $1", goal_id)
    except Exception:
        _log.warning("goal_is_dead: status check failed for %s — treating as alive", goal_id)
        return False
    return status != "active"
