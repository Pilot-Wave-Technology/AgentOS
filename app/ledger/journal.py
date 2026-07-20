"""Core append-only journal with SHA-256 hash chaining and advisory locking.

Aligned with State Ledger spec v0.1, Sections 3, 8.4, and 9.1.

Every event is appended within a transaction that:
  1. Acquires a global advisory lock       (serialises all ledger writes)
  2. Reads the head of the chain           (previous_hash from chain_head)
  3. Computes integrity hash               (SHA-256 of previous_hash + payload)
  4. Inserts the event with full envelope   (idempotency via partial unique index)
  5. Upserts chain_head                    (fast head lookup for next append)
"""
import hashlib
import json
import uuid
from typing import Any, Optional

import asyncpg


try:
    from uuid_utils import uuid7 as _real_uuid7

    def _uuid7() -> uuid.UUID:
        """Generate a time-ordered UUID v7 (time-sortable, globally unique)."""
        return _real_uuid7()
except ImportError:
    def _uuid7() -> uuid.UUID:
        """Fallback: uuid4 when uuid-utils is not installed."""
        return uuid.uuid4()


def generate_event_id() -> uuid.UUID:
    """Public helper — lets callers pre-generate an event_id for use as correlation_id."""
    return _uuid7()


# Single global advisory lock ID for the ledger chain.
GLOBAL_CHAIN_LOCK_ID = 0x4C454447


async def append_event(
    conn: asyncpg.Connection,
    *,
    event_id: Optional[uuid.UUID] = None,
    event_type: str,
    payload: dict[str, Any],
    actor_id: uuid.UUID,
    actor_type: str = "human",
    on_behalf_of: Optional[uuid.UUID] = None,
    envelope_version: int = 1,
    event_version: int = 1,
    correlation_id: Optional[str] = None,
    causation_ids: Optional[list[str]] = None,
    context_task_id: str = "",
    context_goal_id: str = "",
    context_team_id: str = "",
    context_conversation_id: str = "",
    classification: str = "normal",
    extensions: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[dict]:
    """Append one event to the ledger within the caller's transaction.

    Returns {"event_id": ..., "integrity_hash": ...}
    on success, or None if the idempotency_key was already used (duplicate — safe to skip).
    """
    # 0. Provenance audit — warn (never raise) if the actor won't resolve to a
    # registered principal. Done BEFORE the advisory lock so it never extends the
    # ledger critical section; positive-cached so it's ~free after warmup.
    try:
        from app.config import get_settings
        if get_settings().ledger_actor_audit:
            from app.ledger.actors import actor_is_registered
            if not await actor_is_registered(conn, actor_id):
                import logging
                logging.getLogger("app.ledger.audit").warning(
                    "ledger: unregistered actor_id=%s actor_type=%s event=%s — "
                    "attribution will not resolve (see LEDGER_PROVENANCE_REVIEW.md)",
                    actor_id, actor_type, event_type,
                )
    except Exception:
        pass

    # 1. Global advisory lock
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1)", GLOBAL_CHAIN_LOCK_ID,
    )

    # 2. Read head
    row = await conn.fetchrow(
        "SELECT last_hash FROM chain_head WHERE id = 1",
    )
    prev_hash = row["last_hash"] if row else "GENESIS"

    # 3. Compute integrity hash (spec Section 3: SHA-256 of previous_hash + payload)
    event_id = event_id or _uuid7()
    payload_json = json.dumps(payload, sort_keys=True, default=str)
    hash_input = f"{prev_hash}{payload_json}"
    integrity_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    # 4. Insert event with full spec envelope
    corr_uuid = uuid.UUID(str(correlation_id)) if correlation_id else None
    cause_uuids = [uuid.UUID(str(c)) for c in (causation_ids or [])]

    try:
        await conn.execute(
            """INSERT INTO ledger_events
                   (event_id, event_type,
                    envelope_version, event_version,
                    actor_id, actor_type, on_behalf_of,
                    correlation_id, causation_ids,
                    context_task_id, context_goal_id,
                    context_team_id, context_conversation_id,
                    classification,
                    integrity_hash, previous_hash,
                    payload, extensions,
                    idempotency_key)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb,$18::jsonb,$19)""",
            event_id, event_type,
            envelope_version, event_version,
            actor_id, actor_type, on_behalf_of,
            corr_uuid, cause_uuids,
            context_task_id, context_goal_id,
            context_team_id, context_conversation_id,
            classification,
            integrity_hash, prev_hash,
            payload_json, json.dumps(extensions or {}, default=str),
            idempotency_key,
        )
    except asyncpg.UniqueViolationError as exc:
        if "idempotency_key" in str(exc):
            return None  # Duplicate — safe to skip
        raise

    # 5. Upsert chain head (single row, id=1)
    await conn.execute(
        """INSERT INTO chain_head (id, last_event_id, last_hash, event_count)
           VALUES (1, $1, $2, 1)
           ON CONFLICT (id) DO UPDATE SET
               last_event_id = $1,
               last_hash = $2,
               event_count = chain_head.event_count + 1,
               updated_at = now()""",
        event_id, integrity_hash,
    )

    result = {
        "event_id": str(event_id),
        "integrity_hash": integrity_hash,
    }

    # memory_v2 event bridge — best-effort fan-out of whitelisted events to
    # agent memory files. Feature-flagged; no-op when disabled. Scheduled
    # as a background task so it never blocks the ledger write.
    try:
        import asyncio as _asyncio
        from app.memory_v2 import event_bridge as _mv2_bridge
        if _mv2_bridge.is_enabled():
            _asyncio.create_task(_mv2_bridge.maybe_handle_ledger_event({
                "event_id": str(event_id),
                "event_type": event_type,
                "payload": payload or {},
                "context_goal_id": context_goal_id,
                "context_task_id": context_task_id,
            }))
    except Exception:
        # Never let a bridge failure break ledger writes.
        pass

    return result
