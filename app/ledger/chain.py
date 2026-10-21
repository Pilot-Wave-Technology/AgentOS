"""The canonical hash chain and advisory lock specification.

Re-exported from agentos_ledger_client.chain for backward compatibility.
"""
from __future__ import annotations

from agentos_ledger_client.chain import (
    GENESIS,
    GLOBAL_CHAIN_LOCK_ID,
    chain_hash,
    envelope_digest,
    new_event_id,
    payload_json,
)

__all__ = [
    "GENESIS",
    "GLOBAL_CHAIN_LOCK_ID",
    "chain_hash",
    "envelope_digest",
    "new_event_id",
    "payload_json",
]
