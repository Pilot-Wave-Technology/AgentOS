"""The hash chain, defined once.

WHY IT LIVES IN `agentos_ledger_client.chain` AND IS RE-EXPORTED HERE:
Two writers append to this ledger -- the control panel and the kernel --
and both import the canonical primitive from the standalone `agentos-ledger-client`
package. Two definitions of a hash formula that MUST agree byte for byte,
in a structure whose only purpose is detecting when bytes changed, would risk
divergence that presents as a corrupt chain and gets debugged as tampering.

ONE NUMBER, AND IT MUST BE *THIS* NUMBER: 0x4C454447 is "LEDG" in ASCII.
Two appenders holding DIFFERENT advisory locks do not serialise against each
other at all: both read the same chain head, both compute a hash from it, and
the second insert silently forks the chain.

THE HASH INPUT IS `previous_hash + payload`, and nothing else.
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
