"""The canonical hash chain and advisory lock specification for AgentOS State Ledger.

Every ledger writer must use the exact same lock ID and chain formula.
Two writers taking different lock IDs will silently fork the chain, and
divergent hash implementations will falsely indicate tampering.

WHY NOT A SQL FUNCTION, which is the first idea and would be neater:
Postgres cannot reproduce Python's serialisation. `json.dumps(sort_keys=
True)` orders keys lexicographically; `jsonb` orders them by LENGTH first
and then bytewise, so `{"b":1,"aa":2}` serialises in a different order in
each and hashes differently. Every event ever written used the Python
ordering, so THE PYTHON ORDERING IS THE FORMAT.

WHAT THE CHAIN HASH DOES NOT COVER: the actor. Not the actor, not the
event type, not the timestamp. That is a real weakness for a ledger whose
purpose is attribution, and it is deliberately NOT changed here --
thousands of events verify under this rule today and re-defining it
silently would invalidate all of them. `envelope_digest` below is the
forward path: computed and stored alongside, verified when present, so
the chain gains cover over the envelope without a flag day.

BOTH NOTES WERE CARRIED HERE ON PURPOSE. They were in
`kernel/ledger/chain.py` and lost when this module was extracted. In a
package meant to be read by other repositories the next reader is
further from that history, not closer -- which makes this the worst
place to drop the two comments that stop someone "tidying" the hash into
a SQL function or extending it to cover the actor.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

GENESIS = "GENESIS"

# Canonical advisory lock ID for the ledger chain.
# 0x4C454447 is "LEDG" in ASCII.
GLOBAL_CHAIN_LOCK_ID = 0x4C454447


def _sanitize_non_finite_floats(v: Any) -> Any:
    """Recursively encode non-finite floats as canonical strings.

    Leaves all other types untouched so default=str continues handling
    datetime, UUID, Decimal, preserving byte-identity for stored hash
    compatibility.
    """
    if isinstance(v, float) and not math.isfinite(v):
        if math.isnan(v):
            return "NaN"
        return "Infinity" if v > 0 else "-Infinity"
    if isinstance(v, dict):
        return {k: _sanitize_non_finite_floats(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_sanitize_non_finite_floats(val) for val in v]
    if isinstance(v, tuple):
        return tuple(_sanitize_non_finite_floats(val) for val in v)
    return v


def payload_json(payload: Any) -> str:
    """The exact serialized representation of payload bytes in the hash chain.

    sort_keys=True ensures key-order independence.
    default=str ensures dates/UUIDs/decimals serialize rather than raising.
    Non-finite floats are recursively sanitized into canonical strings
    "NaN", "Infinity", and "-Infinity" to survive Postgres jsonb validation.
    """
    target = {} if payload is None else payload
    sanitized = _sanitize_non_finite_floats(target)
    return json.dumps(sanitized, sort_keys=True, default=str)


def chain_hash(previous_hash: str, payload: Any) -> str:
    """SHA-256 over previous_hash concatenated with serialized payload."""
    return hashlib.sha256(
        f"{previous_hash}{payload_json(payload)}".encode()
    ).hexdigest()


def envelope_digest(
    *,
    event_type: str,
    actor_id: Any,
    actor_type: str,
    on_behalf_of: Any = None,
    payload: Any = None,
) -> str:
    """Attribution digest binding envelope metadata and payload together.

    Uses unit separator (\x1f) delimiter between envelope fields.
    """
    parts = [
        str(event_type or ""),
        str(actor_id or ""),
        str(actor_type or ""),
        str(on_behalf_of or ""),
        payload_json(payload),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def new_event_id() -> uuid.UUID:
    """Generate a time-ordered UUID v7 if available, otherwise UUID v4."""
    try:
        from uuid_utils import uuid7 as _real_uuid7

        return uuid.UUID(int=_real_uuid7().int)
    except ImportError:
        factory = getattr(uuid, "uuid7", None)
        return factory() if factory else uuid.uuid4()


__all__ = [
    "GENESIS",
    "GLOBAL_CHAIN_LOCK_ID",
    "payload_json",
    "chain_hash",
    "envelope_digest",
    "new_event_id",
]
