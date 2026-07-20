"""chat_module domain types."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class MessageStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class MessageKind(str, Enum):
    USER = "user"                   # triggers a turn; reply expected
    SYSTEM_WAKE = "system_wake"     # triggers a turn (e.g. go_events wake_up)
    SYSTEM_INJECT = "system_inject" # context only; rides the next turn, no standalone turn

    @property
    def triggers_turn(self) -> bool:
        return self in (MessageKind.USER, MessageKind.SYSTEM_WAKE)


class GatewayStatus(str, Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    DOWN = "down"


@dataclass
class QueuedMessage:
    """One row of the `messages` queue (the inbound side)."""
    id: str
    thread_id: str
    sender: str
    role: str            # 'human' | 'agent'
    kind: MessageKind
    content: str
    status: MessageStatus
    idempotency_key: Optional[str]
    batch_id: Optional[str]
    created_at: datetime


@dataclass
class ThreadContext:
    """Resolved routing context for a thread — joined from threads/agents.

    The `messages` row only stores `thread_id`; everything needed to address
    the gateway (agent id, goal/pid, agent type, owning gateway) is resolved
    per thread at dispatch time.
    """
    thread_id: str
    goal_id: str                 # pid in the session key
    agent_id: str                # CP-side id
    gateway_agent_id: str        # gateway's (often lowercased) id
    agent_type: str              # 'SHCA' | 'GO' | 'IA' | 'Platform'
    gateway_id: str
    gateway_ws_url: str

    @property
    def session_key(self) -> str:
        # Mirrors _AgentSession.send_to_gateway exactly: pid lowercased,
        # gateway agent id as prefix.
        return f"agent:{self.gateway_agent_id}:{self.goal_id.lower()}-{self.thread_id}"

    @property
    def is_group(self) -> bool:
        # GO and the Platform steward are both GO-shaped goal agents that own the
        # goal thread → dispatch via ensure_go_session so the reply persists +
        # broadcasts to the goal thread (the steward IS the goal's go_agent).
        return self.agent_type.upper() in ("GO", "PLATFORM")


@dataclass
class Gateway:
    id: str
    ws_url: str
    status: GatewayStatus
    weight: int = 1
    max_agents: int = 1000
    current_agents: int = 0
    name: Optional[str] = None
    internal_ip: Optional[str] = None

    @property
    def has_capacity(self) -> bool:
        return self.status == GatewayStatus.ACTIVE and self.current_agents < self.max_agents
