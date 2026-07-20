"""Surface engine GitHub-op failures on the node so they show in the git-engine UI.

The git-engine page renders `git_nodes.state` + the `error` column (= `last_error`).
A GitHub failure that is only logged leaves the node looking clean while it's
silently stuck/looping. Every LIVE pass (create / detect / grind) routes its
failures through `record_node_github_failure` so nothing is invisible:

  401 / 403 / 404  → ESCALATED + last_error  (auth/permission/gone — does NOT
                     self-heal; free the lease slot so the lane isn't stuck behind
                     it; a human must fix the App grant / config and re-queue).
  everything else  → keep the node's state + lease, SET last_error (visible) and let
                     the pass retry next tick (transient 5xx / 429 / network / bug).

`clear_node_error` wipes a stale `last_error` once a node makes clean progress, so a
recovered node doesn't keep showing an old error.
"""
from __future__ import annotations

import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

# Codes that won't fix themselves without a human (missing App permission, revoked
# install, deleted ref/PR) → escalate + hold. Everything else is retried.
_ESCALATE_CODES = (401, 403, 404)


def _status_code(exc: Exception) -> int:
    resp = getattr(exc, "response", None)
    return int(getattr(resp, "status_code", 0) or 0) if resp is not None else 0


async def record_node_github_failure(node_id: str, exc: Exception, *, context: str) -> str:
    """Classify a GitHub failure for `node_id` and surface it on the node.

    Returns 'escalated' (held, needs a human) or 'transient' (left to retry).
    Never raises — surfacing must not itself crash a pass."""
    code = _status_code(exc)
    msg = f"{context}: GitHub HTTP {code or 'transport'} — {str(exc)[:300]}"
    pool = get_pool()
    try:
        if code in _ESCALATE_CODES:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "UPDATE git_nodes SET state = 'ESCALATED', last_error = $2, "
                    "updated_at = now() WHERE id = $1 AND state NOT IN ('MERGED', 'ABANDONED')",
                    node_id, msg[:500],
                )
                # Free the lease slot so the lane promotes the next node instead of
                # stalling behind a node that can't progress without human action.
                await conn.execute(
                    "UPDATE git_merge_queue SET status = 'parked' "
                    "WHERE node_id = $1 AND status IN ('active', 'waiting')",
                    node_id,
                )
            _log.warning("git-engine: node %s ESCALATED — %s", node_id, msg)
            return "escalated"
        # Transient / unexpected: keep state + lease, surface the reason, retry next tick.
        await pool.execute(
            "UPDATE git_nodes SET last_error = $2, updated_at = now() WHERE id = $1",
            node_id, msg[:500],
        )
        _log.warning("git-engine: node %s transient failure (will retry) — %s", node_id, msg)
        return "transient"
    except Exception:
        # The DB write itself failed — fall back to a log so the failure isn't lost.
        _log.exception("git-engine: failed to surface node %s error (%s)", node_id, msg)
        return "transient"


async def clear_node_error(executor, node_id: str) -> None:
    """Clear a stale `last_error` after a node makes clean progress. `executor` may be
    a pool or a connection. No-op if there's nothing to clear. Never raises."""
    try:
        await executor.execute(
            "UPDATE git_nodes SET last_error = NULL, updated_at = now() "
            "WHERE id = $1 AND last_error IS NOT NULL",
            node_id,
        )
    except Exception:
        _log.exception("git-engine: failed to clear node %s last_error", node_id)
