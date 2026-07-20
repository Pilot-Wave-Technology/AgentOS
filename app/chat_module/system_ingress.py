"""System-message ingress into the durable chat queue.

System events (resource refreshes, task/story state notes) historically wrote
straight to the gateway transcript via `chat.inject` — bypassing the per-thread
advisory lock the dispatcher uses to serialize turns. That let them land
mid-turn (where an in-turn compaction could clobber them) and never persist to
the `messages` table.

Routing them through the queue as SYSTEM_INJECT instead makes them durable,
ordered, and serialized with everything else on the thread: the dispatcher
folds pending injects into the NEXT turn's batch under the advisory lock
(`select_pending_batch` returns all queued rows; `collect.has_turn_trigger`
keeps a pure-inject batch from running a standalone turn). They ride the next
real turn — same semantics as chat.inject — but safely. They are hidden from
the chat UI by the `kind <> 'system_inject'` display filter.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import repository as repo
from .config import get_chat_config
from .models import MessageKind

_log = logging.getLogger(__name__)


async def enqueue_system_inject(
    *, thread_id: str, goal_id: str, agent_id: str,
    gateway_agent_id: str, agent_type: str, content: str,
) -> Optional[str]:
    """Enqueue a SYSTEM_INJECT for a thread.

    Upserts routing first so the dispatcher can resolve the agent even with no
    warm session. Returns the new message id, or None if the queue is disabled
    (caller should fall back to the legacy chat.inject path).
    """
    if not get_chat_config().queue_enabled:
        return None
    if not thread_id or not content:
        return None
    await repo.upsert_thread_routing(
        thread_id=thread_id, goal_id=goal_id, agent_id=agent_id,
        gateway_agent_id=gateway_agent_id, agent_type=agent_type, gateway_id=None,
    )
    return await repo.enqueue(
        thread_id=thread_id, sender="system", role="human",
        kind=MessageKind.SYSTEM_INJECT, content=content,
    )


async def enqueue_system_wake(
    *, thread_id: str, goal_id: str, agent_id: str,
    gateway_agent_id: str, agent_type: str, content: str,
) -> Optional[str]:
    """Enqueue a SYSTEM_WAKE for a thread — a system-initiated message that runs
    its OWN turn (e.g. a memory flush), rather than riding the next one like
    SYSTEM_INJECT. It serializes through the same per-thread advisory lock the
    dispatcher uses (so it never overlaps a user turn) and, thanks to FCFS, is
    its own turn in arrival order. Hidden from the chat UI by the display filter.

    Upserts routing first so the dispatcher can resolve the agent with no warm
    session. Returns the message id, or None if the queue is disabled (caller
    should fall back to the direct admin-RPC path).
    """
    if not get_chat_config().queue_enabled:
        return None
    if not thread_id or not content:
        return None
    await repo.upsert_thread_routing(
        thread_id=thread_id, goal_id=goal_id, agent_id=agent_id,
        gateway_agent_id=gateway_agent_id, agent_type=agent_type, gateway_id=None,
    )
    return await repo.enqueue(
        thread_id=thread_id, sender="system", role="human",
        kind=MessageKind.SYSTEM_WAKE, content=content,
    )
