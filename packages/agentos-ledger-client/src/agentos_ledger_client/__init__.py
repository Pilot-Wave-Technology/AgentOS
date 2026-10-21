"""AgentOS State Ledger Client Package.

Lightweight client and content-addressed blob storage for the AgentOS State Ledger.
"""

from __future__ import annotations

from agentos_ledger_client.blobs import (
    INLINE_THRESHOLD_BYTES,
    STORAGE_GCS,
    STORAGE_INLINE,
    BlobRef,
    get_blob_ref,
    get_blob_ref_by_sha256,
    read_blob,
    read_blob_by_sha256,
    store_blob,
)
from agentos_ledger_client.bootstrap import init_schema, schema_sql
from agentos_ledger_client.chain import (
    GENESIS,
    GLOBAL_CHAIN_LOCK_ID,
    chain_hash,
    envelope_digest,
    new_event_id,
    payload_json,
)
from agentos_ledger_client.agent_os_client import AgentOSClient, AgentOSClientError
from agentos_ledger_client.client import LedgerClient
from agentos_ledger_client.journal import (
    append_event,
    generate_event_id,
    read_events,
    register_post_append_hook,
    register_pre_append_hook,
    verify_chain,
)
from agentos_ledger_client.types import (
    AppendResult,
    ChainHead,
    ChainVerification,
    LedgerEvent,
)

__version__ = "0.1.0"

__all__ = [
    "init_schema",
    "schema_sql",
    # Chain & Lock
    "GENESIS",
    "GLOBAL_CHAIN_LOCK_ID",
    "chain_hash",
    "envelope_digest",
    "new_event_id",
    "payload_json",
    # Journal
    "append_event",
    "generate_event_id",
    "read_events",
    "register_pre_append_hook",
    "register_post_append_hook",
    "verify_chain",
    # Blobs
    "BlobRef",
    "store_blob",
    "read_blob",
    "get_blob_ref",
    "get_blob_ref_by_sha256",
    "read_blob_by_sha256",
    "INLINE_THRESHOLD_BYTES",
    "STORAGE_INLINE",
    "STORAGE_GCS",
    # Client
    "LedgerClient",
    "AgentOSClient",
    "AgentOSClientError",
    # Types
    "AppendResult",
    "ChainHead",
    "ChainVerification",
    "LedgerEvent",
]
