"""State Ledger — append-only event journal with hash chaining and client SDK.

Public API:
    emit_event()  — fire-and-forget wrapper (never raises)
    append_event() — core journal append (use inside a transaction)
    BlobRef       — content-addressed binary blob reference
    store_blob()  — store blob inline or in GCS with sha256 dedup
    read_blob()   — retrieve blob bytes with integrity validation
"""
from app.ledger.actors import (
    ACTOR_GO,
    ACTOR_HUMAN,
    ACTOR_IA,
    ACTOR_SHCA,
    ACTOR_SYSTEM,
    SYSTEM_ACTOR_ID,
    actor_is_registered,
    agent_actor,
    go_actor,
    human_actor,
    instance_actor,
    principal_actor,
    register_agent_instance_actor,
    request_actor,
    story_shca_actor,
    system_actor,
)
from app.ledger.blobs import (
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
from app.ledger.chain import (
    GENESIS,
    GLOBAL_CHAIN_LOCK_ID,
    chain_hash,
    envelope_digest,
    new_event_id,
    payload_json,
)
from app.ledger.client import LedgerClient
from app.ledger.emit import emit_event
from app.ledger.journal import append_event
from app.ledger.types import AppendResult, ChainHead, LedgerEvent

__all__ = [
    "emit_event",
    "append_event",
    "BlobRef",
    "store_blob",
    "read_blob",
    "get_blob_ref",
    "get_blob_ref_by_sha256",
    "read_blob_by_sha256",
    "INLINE_THRESHOLD_BYTES",
    "STORAGE_INLINE",
    "STORAGE_GCS",
    "SYSTEM_ACTOR_ID",
    "ACTOR_HUMAN",
    "ACTOR_SHCA",
    "ACTOR_IA",
    "ACTOR_GO",
    "ACTOR_SYSTEM",
    "system_actor",
    "human_actor",
    "agent_actor",
    "go_actor",
    "instance_actor",
    "story_shca_actor",
    "principal_actor",
    "register_agent_instance_actor",
    "actor_is_registered",
    "request_actor",
    "GLOBAL_CHAIN_LOCK_ID",
    "GENESIS",
    "chain_hash",
    "envelope_digest",
    "new_event_id",
    "payload_json",
    "LedgerClient",
    "AppendResult",
    "ChainHead",
    "LedgerEvent",
]


# ── THE HOST'S WIRING, HANDED TO THE CLIENT ONCE ─────────────────────
#
# The package used to reach back into `app.*` for these -- settings, GCS
# helpers, the actor registry, the memory bridge -- each inside an
# `except Exception` so it "worked" elsewhere. It did not work: on a
# database with only the ledger tables the actor lookup
# (`SELECT 1 FROM actors ...`) failed, the except swallowed it, and the
# caller's transaction was already poisoned, so the append died with
# `InFailedSQLTransactionError`. See
# `agentos_ledger_client/integration.py`.
#
# So the direction is reversed: nothing is imported from here, and this
# module -- which every ledger caller in the control panel already loads
# -- hands the client what agentos can provide. A consumer hands in
# nothing and gets a package that does less, not one that breaks.
#
# EVERY HOOK IS LAZY. `configure` stores callables, and each is resolved
# at call time, so importing the ledger does not import GCS or read
# settings.
def _wire_the_client() -> None:
    from agentos_ledger_client.integration import configure

    async def _actor_check(conn, actor_id) -> bool:
        """The audit, only while the setting is on. Returning True when
        it is off means `append_event` logs nothing, which is what
        'disabled' meant before."""
        from app.config import get_settings
        if not get_settings().ledger_actor_audit:
            return True
        from app.ledger.actors import actor_is_registered
        return await actor_is_registered(conn, actor_id)

    def _gcs_client():
        from app.storage import get_gcs_client
        return get_gcs_client()

    async def _download(uri: str) -> bytes:
        from app.storage import download_gcs_uri_bytes
        return await download_gcs_uri_bytes(uri)

    async def _on_event(event: dict) -> None:
        from app.memory_v2 import event_bridge
        if event_bridge.is_enabled():
            await event_bridge.maybe_handle_ledger_event(event)

    try:
        from app.config import get_settings
        bucket = get_settings().gcs_resource_bucket
    except Exception:                                       # noqa: BLE001
        # Settings may not be loadable at import time in every entry
        # point; the bucket is only needed for a blob over 1MB, and
        # `resource_bucket_name` falls back to GCS_RESOURCE_BUCKET and
        # then raises. Never fatal here.
        bucket = ""

    configure(bucket_name=bucket, gcs_client=_gcs_client,
              download=_download, actor_check=_actor_check,
              on_event=_on_event)


_wire_the_client()
