"""Core append-only journal with SHA-256 hash chaining and advisory locking.

Re-exports core primitives from agentos_ledger_client.journal with full backward compatibility.
"""
from __future__ import annotations

from agentos_ledger_client.chain import GLOBAL_CHAIN_LOCK_ID
from agentos_ledger_client.journal import (
    append_event,
    generate_event_id,
    register_post_append_hook,
    register_pre_append_hook,
)

__all__ = [
    "GLOBAL_CHAIN_LOCK_ID",
    "generate_event_id",
    "append_event",
    "register_pre_append_hook",
    "register_post_append_hook",
]
