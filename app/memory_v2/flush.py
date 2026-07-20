"""Flush trigger helpers for story-complete fold + pre-reap flush.

Both fire a synthetic agent turn ("silent flush") that prompts the agent
to write durable memory before something irreversible happens (story
closes / SHCA gets reaped). The agent's reply is suppressed in the chat
UI by the [NO_REPLY] convention enforced in the flush prompt itself.

The pre-compaction flush is handled natively by OpenClaw at the token
threshold — we don't trigger it from here. Only story-complete and
pre-reap are control-panel-driven.

Feature-flagged via settings.memory_v2_flush_enabled (default False).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import get_settings
from app.database import get_pool
from app.memory_v2.prompts import get_active_prompt, render_prompt


_log = logging.getLogger(__name__)


# Allowed flush_type values.
FLUSH_TYPES = ("story_complete", "pre_reap", "manual", "auto")

# Hard timeout for a flush turn — if the agent doesn't finish in this many
# seconds, we give up and proceed (reap can't be blocked forever).
_FLUSH_TIMEOUT_SECONDS = 30.0


def is_enabled() -> bool:
    return bool(getattr(get_settings(), "memory_v2_flush_enabled", False))


# ── Public API ──────────────────────────────────────────────────────────────

async def trigger_story_complete_flush(
    *, agent_id: str, agent_type: str, thread_id: str,
    goal_id: str, story_id: str, story_title: str, final_state: str,
) -> bool:
    """Fire a story-complete flush for one agent (SHCA or GO).

    Returns True if the flush turn was sent (regardless of agent reply).
    """
    if not is_enabled():
        return False
    if agent_type not in ("SHCA", "GO"):
        _log.warning("trigger_story_complete_flush: invalid agent_type=%r", agent_type)
        return False

    context = {
        "story_id": story_id,
        "story_title": story_title or "",
        "final_state": final_state,
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    return await _send_flush_turn(
        agent_id=agent_id, agent_type=agent_type, thread_id=thread_id,
        goal_id=goal_id, story_id=story_id,
        flush_type="story_complete",
        user_prompt_kind="story_complete_user_prompt",
        system_prompt_kind="story_complete_system_prompt",
        context=context,
    )


async def trigger_manual_flush(
    *, agent_id: str, agent_type: str, thread_id: str,
    goal_id: Optional[str],
) -> bool:
    """Fire a flush turn manually (UI button). Reuses the agent's normal
    pre-compaction flush prompts since that's the most generic kind:
    "write durable memory now, on demand."

    Bypasses the is_enabled() check so testing works regardless of the
    flush feature flag — the manual button is the user's explicit ask.
    Returns True if the synthetic turn was queued, False if the session
    isn't live.
    """
    if agent_type not in ("SHCA", "GO"):
        _log.warning("trigger_manual_flush: invalid agent_type=%r", agent_type)
        return False

    context = {
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    return await _send_flush_turn(
        agent_id=agent_id, agent_type=agent_type, thread_id=thread_id,
        goal_id=goal_id, story_id=None,
        flush_type="manual",
        user_prompt_kind="flush_user_prompt",
        system_prompt_kind="flush_system_prompt",
        context=context,
    )


async def trigger_auto_flush(
    *, agent_id: str, agent_type: str, thread_id: str,
    goal_id: Optional[str],
) -> bool:
    """Fire a flush turn automatically when the token-usage threshold has
    been crossed. Reuses the agent's normal pre-compaction flush prompts
    (same as manual flush) — admin-editable per agent_type via the
    memory_prompts table.

    Hooked from openclaw.py's chat:final handler: after each agent reply
    persists, we check sessions.status against memoryFlushLastFiredAtTokens
    + softThresholdTokens and dispatch this if (a) threshold crossed and
    (b) gate is armed (null). The openclaw-side gate set on the flush
    turn's completion prevents re-fire until the next compaction resets
    memoryFlushLastFiredAtTokens to null.

    Bypasses is_enabled() because the auto-trigger IS the design intent
    of the v2 flush system — the feature flag was meant for the
    experimental phase. Returns True if dispatched, False if not.
    """
    if agent_type not in ("SHCA", "GO"):
        _log.warning("trigger_auto_flush: invalid agent_type=%r", agent_type)
        return False

    context = {
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    return await _send_flush_turn(
        agent_id=agent_id, agent_type=agent_type, thread_id=thread_id,
        goal_id=goal_id, story_id=None,
        flush_type="auto",
        user_prompt_kind="flush_user_prompt",
        system_prompt_kind="flush_system_prompt",
        context=context,
    )


async def trigger_pre_reap_flush(
    *, agent_id: str, agent_type: str, thread_id: str,
    goal_id: Optional[str],
) -> bool:
    """Fire a pre-reap flush for an SHCA. (GOs aren't reaped this way.)

    Best-effort with a hard 30s timeout — the cleanup watchdog must
    continue regardless.
    """
    if not is_enabled():
        return False
    if agent_type != "SHCA":
        return False

    context = {
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    try:
        return await asyncio.wait_for(
            _send_flush_turn(
                agent_id=agent_id, agent_type=agent_type, thread_id=thread_id,
                goal_id=goal_id, story_id=None,
                flush_type="pre_reap",
                user_prompt_kind="pre_reap_user_prompt",
                system_prompt_kind="pre_reap_system_prompt",
                context=context,
            ),
            timeout=_FLUSH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _log.warning(
            "pre_reap_flush: timed out for agent=%s — proceeding with reap anyway",
            agent_id,
        )
        return False


# ── Internal ────────────────────────────────────────────────────────────────

async def _send_flush_turn(
    *, agent_id: str, agent_type: str, thread_id: str,
    goal_id: Optional[str], story_id: Optional[str],
    flush_type: str,
    user_prompt_kind: str, system_prompt_kind: str,
    context: dict,
) -> bool:
    """Compose + send the flush turn through the gateway.

    The flush is a normal chat.send with a system-flagged message. The
    agent's reply starts with [NO_REPLY] per the prompt convention, and
    our chat UI filters those out (no user-visible turn).
    """
    user_template = await get_active_prompt(agent_type, user_prompt_kind)
    if not user_template:
        _log.warning(
            "flush: no active %s/%s prompt — skipping flush_type=%s",
            agent_type, user_prompt_kind, flush_type,
        )
        return False
    system_template = await get_active_prompt(agent_type, system_prompt_kind)

    user_prompt = render_prompt(user_template, context)
    system_prompt = render_prompt(system_template or "", context) if system_template else ""

    # Send via the gateway's admin RPC (gateway_admin.call("agent", ...))
    # — same path task_controller uses to dispatch IA tasks. Does NOT
    # require a CP-side _AgentSession or any browser tab to be open. The
    # agent writes to memory files as side effects of tool calls during
    # the turn; we don't wait for the turn to complete (besides the
    # pre_reap caller's outer wait_for).
    try:
        from app.openclaw import session_manager
        idempotency_key = f"flush-{flush_type}-{agent_id}-{uuid.uuid4().hex[:8]}"
        # Prefer the durable queue: the flush becomes a SYSTEM_WAKE turn that
        # always runs (reaper-retried, never dropped) and serializes with the
        # dispatcher's per-thread lock so it never overlaps a user turn. Falls
        # back to the direct admin RPC when the queue is off or routing can't be
        # resolved.
        sent = await session_manager.enqueue_silent_flush(
            goal_id=goal_id or "",
            agent_id=agent_id,
            agent_type=agent_type,
            thread_id=thread_id,
            user_message=user_prompt,
            system_prompt=system_prompt,
        )
        if not sent:
            sent = await session_manager.send_silent_flush(
                goal_id=goal_id or "",
                agent_id=agent_id,
                thread_id=thread_id,
                user_message=user_prompt,
                system_prompt=system_prompt,
                idempotency_key=idempotency_key,
            )
        if not sent:
            # Admin RPC dispatch failed — typically gateway-side agent
            # resolution failed (no active instance) or the agent RPC
            # raised. Emit a *_dropped ledger event so observability is
            # honest instead of pretending the flush succeeded.
            _log.warning(
                "flush DROPPED — admin RPC dispatch failed: type=%s agent=%s thread=%s story=%s",
                flush_type, agent_id, thread_id, story_id,
            )
            await _emit_flush_ledger_event(
                agent_id=agent_id, agent_type=agent_type,
                goal_id=goal_id, story_id=story_id,
                flush_type=f"{flush_type}_dropped",
            )
            return False
        await _emit_flush_ledger_event(
            agent_id=agent_id, agent_type=agent_type,
            goal_id=goal_id, story_id=story_id,
            flush_type=flush_type,
        )
        _log.info(
            "flush sent: type=%s agent=%s thread=%s story=%s",
            flush_type, agent_id, thread_id, story_id,
        )
        return True
    except Exception:
        _log.exception(
            "flush: send failed (type=%s agent=%s)", flush_type, agent_id,
        )
        return False




async def _emit_flush_ledger_event(
    *, agent_id: str, agent_type: str,
    goal_id: Optional[str], story_id: Optional[str],
    flush_type: str,
) -> None:
    """Record `memory.flush` ledger event for observability."""
    try:
        from app.ledger.journal import append_event
        from app.ledger.actors import SYSTEM_ACTOR_ID
        pool = get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await append_event(
                conn,
                actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                event_type="memory.flush",
                payload={
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "flush_type": flush_type,
                    "story_id": story_id,
                },
                context_goal_id=goal_id,
            )
    except Exception:
        _log.exception("flush ledger emit failed (type=%s agent=%s)", flush_type, agent_id)
