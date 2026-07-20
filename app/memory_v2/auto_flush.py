"""Automatic memory-flush dispatcher.

Hooked from openclaw.py's chat:final handler. After every agent reply
persists, we check the session's context-usage state and dispatch an
auto-flush when both:

  - totalTokens >= flushThresholdPct × contextTokens
    (simplified 2026-05-26 from the older
     contextWindow - reserve - softThreshold math)
  - cycle gate is armed: compactionCount > last-dispatched-compactionCount
    (one flush per compaction cycle — the user's stated design)

The actual flush turn is dispatched via trigger_auto_flush which
re-uses the manual-flush prompt path (flush_user_prompt +
flush_system_prompt from memory_prompts, admin-editable per agent_type).

Cycle behavior (the user's design intent, restated):

  initial:                  compactionCount=0, _last_dispatch=None
  → conversation past 30%   → dispatch  → _last_dispatch=0
  → next poll               → gate blocked (0 > 0 is false)
  → compaction fires        → compactionCount=1, _last_dispatch=0
  → next poll               → gate armed (1 > 0)
  → conversation past 30%   → dispatch  → _last_dispatch=1
  → compaction fires        → compactionCount=2
  → cycle continues

Gate state is in-memory and per-process. Lost on CP restart — first
poll after restart fires one "extra" flush, then the cycle stabilizes.
Acceptable trade-off vs. plumbing memoryFlushCount writes through the
gateway "agent" RPC.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.openclaw import make_goal_session_key

_log = logging.getLogger(__name__)

# Per-process map of session_key → compactionCount-value-at-last-dispatch.
# The gate compares current compactionCount > _last_dispatch[session_key]
# to decide whether the cycle has advanced.
_last_dispatch_at_compaction: dict[str, int] = {}

# In-flight dedupe map (separate from the cycle gate). Prevents two
# chat:final events landing within seconds from firing two flushes
# while the first is still dispatching. 60s TTL.
_inflight: dict[str, float] = {}
_INFLIGHT_TTL_SECONDS = 60.0

# Fallback when openclaw doesn't return flushThresholdPct (older deploys).
_DEFAULT_FLUSH_THRESHOLD_PCT = 0.70


def _gc_inflight(now: float) -> None:
    stale = [k for k, t in _inflight.items() if now - t > _INFLIGHT_TTL_SECONDS]
    for k in stale:
        _inflight.pop(k, None)


async def maybe_dispatch_auto_flush(
    *,
    pid: str,
    agent_id: str,
    agent_type: str,
    thread_id: str,
) -> bool:
    """Conditionally dispatch an auto-flush turn for one session.

    Designed to be called as a fire-and-forget asyncio.create_task from
    the chat:final handler. Never raises — any failure is logged and
    swallowed so chat flow continues normally.

    Returns True only if a flush was dispatched; False (and a debug log)
    in all skip cases.
    """
    if not pid or not agent_id:
        return False
    if agent_type not in ("SHCA", "GO"):
        # Other agent types (IA, task-reviewer, etc.) don't currently
        # have memory_v2 prompts — skip silently.
        return False
    if not thread_id:
        return False

    session_key = make_goal_session_key(agent_id, pid, thread_id)

    now = time.monotonic()
    _gc_inflight(now)
    if session_key in _inflight:
        # In-flight from a recent chat:final — don't double-dispatch.
        return False

    try:
        from app.openclaw import gateway_admin, GatewayError
        try:
            status = await gateway_admin.call(
                "sessions.status", {"key": session_key}, timeout=10,
            )
        except GatewayError as exc:
            _log.debug(
                "auto_flush: sessions.status failed for %s: %s",
                session_key, exc,
            )
            return False

        if not status or not status.get("ok"):
            return False

        total_tokens = status.get("totalTokens")
        context_tokens = status.get("contextTokens")
        # Pct-based threshold. Older openclaw builds don't return this
        # field; default to 0.3 to match the simplified design.
        flush_pct = status.get("flushThresholdPct") or _DEFAULT_FLUSH_THRESHOLD_PCT
        compaction_count = status.get("compactionCount", 0) or 0

        if not isinstance(total_tokens, (int, float)) or total_tokens <= 0:
            return False
        if not isinstance(context_tokens, (int, float)) or context_tokens <= 0:
            return False
        if not isinstance(flush_pct, (int, float)) or flush_pct <= 0 or flush_pct > 1:
            return False
        if not isinstance(compaction_count, (int, float)):
            compaction_count = 0
        compaction_count = int(compaction_count)

        threshold = int(context_tokens * flush_pct)
        if total_tokens < threshold:
            return False

        # Cycle gate: only dispatch if compactionCount has advanced
        # since the last dispatch (or this is the first ever).
        last_dispatch = _last_dispatch_at_compaction.get(session_key)
        if last_dispatch is not None and compaction_count <= last_dispatch:
            return False  # already dispatched for this cycle

        _inflight[session_key] = now
        _log.info(
            "auto_flush: dispatching for session=%s totalTokens=%d "
            "threshold=%d (%.0f%% of %d) compactionCount=%d lastDispatch=%s",
            session_key, int(total_tokens), threshold, flush_pct * 100,
            int(context_tokens), compaction_count,
            "None" if last_dispatch is None else str(last_dispatch),
        )

        from app.memory_v2 import flush as _flush
        ok = await _flush.trigger_auto_flush(
            agent_id=agent_id, agent_type=agent_type,
            thread_id=thread_id, goal_id=pid,
        )
        if not ok:
            _log.warning(
                "auto_flush: dispatch returned False for session=%s "
                "(prompt missing or session not live)",
                session_key,
            )
            _inflight.pop(session_key, None)
            return False

        # Update the cycle gate so the next poll skips until the next
        # compaction advances compactionCount.
        _last_dispatch_at_compaction[session_key] = compaction_count
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception(
            "auto_flush: unexpected error for session=%s: %s",
            session_key, exc,
        )
        _inflight.pop(session_key, None)
        return False


def clear_state(session_key: Optional[str] = None) -> None:
    """Operator unblock — clear the in-memory gate state. Used by tests
    and any future admin RPC. With no session_key, clears all sessions."""
    if session_key is None:
        _last_dispatch_at_compaction.clear()
        _inflight.clear()
    else:
        _last_dispatch_at_compaction.pop(session_key, None)
        _inflight.pop(session_key, None)
