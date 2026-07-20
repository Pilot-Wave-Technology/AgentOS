"""State Ledger — append-only event journal with hash chaining.

Public API:
    emit_event()  — fire-and-forget wrapper (never raises)
    append_event() — core journal append (use inside a transaction)
"""
from app.ledger.emit import emit_event
from app.ledger.journal import append_event
from app.ledger.actors import (
    SYSTEM_ACTOR_ID,
    ACTOR_HUMAN, ACTOR_SHCA, ACTOR_IA, ACTOR_GO, ACTOR_SYSTEM,
    system_actor, human_actor, agent_actor, go_actor, instance_actor,
    story_shca_actor, principal_actor,
    register_agent_instance_actor, actor_is_registered, request_actor,
)

__all__ = [
    "emit_event", "append_event",
    "SYSTEM_ACTOR_ID",
    "ACTOR_HUMAN", "ACTOR_SHCA", "ACTOR_IA", "ACTOR_GO", "ACTOR_SYSTEM",
    "system_actor", "human_actor", "agent_actor", "go_actor", "instance_actor",
    "story_shca_actor",
    "principal_actor", "register_agent_instance_actor", "actor_is_registered",
    "request_actor",
]
