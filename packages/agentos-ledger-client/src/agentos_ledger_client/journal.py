"""Core append-only journal with SHA-256 hash chaining and advisory locking.

Aligned with State Ledger spec v0.1, Sections 3, 8.4, and 9.1.

Every event is appended within a transaction that:
  1. Acquires a global advisory lock       (serialises all ledger writes)
  2. Reads the head of the chain           (previous_hash from chain_head)
  3. Computes integrity hash               (SHA-256 of previous_hash + payload)
  4. Inserts the event with full envelope   (idempotency via partial unique index)
  5. Upserts chain_head                    (fast head lookup for next append)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any, AsyncIterator, Callable, Optional

import asyncpg

from agentos_ledger_client.integration import current
from agentos_ledger_client.chain import (
    GENESIS,
    GLOBAL_CHAIN_LOCK_ID,
    chain_hash,
    new_event_id,
    payload_json as _payload_json,
)
from agentos_ledger_client.types import ChainVerification, LedgerEvent

_pre_append_hooks: list[Callable[..., Any]] = []
_post_append_hooks: list[Callable[..., Any]] = []


def register_pre_append_hook(hook: Callable[..., Any]) -> None:
    """Register a hook called before advisory lock acquisition."""
    _pre_append_hooks.append(hook)


def register_post_append_hook(hook: Callable[..., Any]) -> None:
    """Register a hook called after event insertion."""
    _post_append_hooks.append(hook)


def generate_event_id() -> uuid.UUID:
    """Public helper — lets callers pre-generate an event_id for use as correlation_id."""
    return new_event_id()


async def _swallow(awaitable) -> None:
    """Run a detached hook and drop its failure.

    A hook is an observer: it must not be able to fail an append that
    already committed, and nothing is waiting on its result. Logged
    rather than silent so a broken hook is findable.
    """
    try:
        await awaitable
    except Exception:                                       # noqa: BLE001
        logging.getLogger(__name__).warning(
            "ledger post-append hook failed", exc_info=True)


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
    # 0. Pre-append hooks / Provenance audit
    for hook in _pre_append_hooks:
        try:
            res = hook(conn=conn, actor_id=actor_id, actor_type=actor_type, event_type=event_type)
            if hasattr(res, "__await__"):
                await res
        except Exception:
            pass

    # THE ATTRIBUTION AUDIT, AND WHY IT IS NO LONGER AN IMPORT.
    #
    # This read `app.ledger.actors.actor_is_registered`, which runs
    # `SELECT 1 FROM actors WHERE id = $1`, inside the caller's
    # transaction and inside `except Exception: pass`. On a database
    # holding only the ledger tables -- a consumer's -- there is no
    # `actors` table. The query failed, the except swallowed it, and
    # Postgres had already POISONED THE TRANSACTION, so the INSERT below
    # died with `InFailedSQLTransactionError` naming neither cause nor
    # table. An audit nobody asked for broke the append they did.
    #
    # It is now the host's callable or nothing at all. No check is
    # configured -> no statement is issued -> no transaction to poison.
    # And the exception is NOT swallowed: a host that supplies a broken
    # check should hear about it, and it can no longer be a consumer's
    # problem.
    actor_check = current().actor_check
    if actor_check is not None and not await actor_check(conn, actor_id):
        logging.getLogger("agentos_ledger_client.audit").warning(
            "ledger: unregistered actor_id=%s actor_type=%s event=%s — "
            "attribution will not resolve", actor_id, actor_type, event_type)

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
    event_id = event_id or new_event_id()
    payload_str = _payload_json(payload)
    integrity_hash = chain_hash(prev_hash, payload)

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
            payload_str, json.dumps(extensions or {}, default=str),
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

    # 6. Post-append hooks / event bridges
    #
    # DETACHED, NOT AWAITED, AND THIS IS THE WHOLE POINT OF THE CHANGE.
    #
    # `pg_advisory_xact_lock` is TRANSACTION-scoped: it is held until the
    # caller commits, and everything in this function runs before that.
    # Awaiting a hook here therefore held the ONE global ledger lock --
    # the lock every writer in the system serialises on -- for however
    # long somebody else's callback took. A hook that does any I/O would
    # have made itself a bottleneck for every ledger append anywhere,
    # which is the opposite of what an extension point should cost.
    #
    # Nothing registers a hook today, so this is cheap to get right now
    # and expensive to discover later. The `memory_v2` bridge directly
    # below has always been fired as a detached task for exactly this
    # reason; the hooks now match it.
    #
    # The consequence, stated rather than hidden: a hook can no longer
    # abort or amend the append, and its exception reaches the task, not
    # this caller. A hook that must run INSIDE the transaction is a
    # different feature, and it should take the lock cost deliberately.
    for hook in _post_append_hooks:
        try:
            res = hook(
                event_id=event_id,
                event_type=event_type,
                payload=payload,
                context_goal_id=context_goal_id,
                context_task_id=context_task_id,
            )
            if hasattr(res, "__await__"):
                import asyncio as _aio
                _aio.create_task(_swallow(res))
        except Exception:
            pass

    # The host's event sink -- agentos wires its memory bridge here.
    # Detached for the same reason the hooks above are: this still runs
    # before the caller commits, so the global ledger lock is held.
    sink = current().on_event
    if sink is not None:
        import asyncio as _asyncio
        _asyncio.create_task(_swallow(sink({
            "event_id": str(event_id),
            "event_type": event_type,
            "payload": payload or {},
            "context_goal_id": context_goal_id,
            "context_task_id": context_task_id,
        })))

    return result


def _as_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else (v or {})


def _is_uuid(val: Any) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def _resolve_bound(conn: Any, val: Any) -> tuple[str, Any]:
    """Resolve bound value to ('seq', int) or ('time', datetime)."""
    if isinstance(val, int):
        return "seq", val
    if isinstance(val, (datetime, date)):
        return "time", val
    if isinstance(val, uuid.UUID) or (isinstance(val, str) and _is_uuid(val)):
        u = uuid.UUID(str(val))
        row = await conn.fetchrow(
            "SELECT sequence_number FROM ledger_events WHERE event_id = $1", u
        )
        if row is not None:
            return "seq", row["sequence_number"]
        return "seq", 0
    if isinstance(val, str):
        if val.isdigit():
            return "seq", int(val)
        try:
            return "time", datetime.fromisoformat(val)
        except ValueError:
            pass
    return "time", val


def _row_to_ledger_event(row: Any) -> LedgerEvent:
    payload = _as_dict(row["payload"])
    extensions = _as_dict(row["extensions"]) if row["extensions"] is not None else None
    causation_ids = (
        list(row["causation_ids"]) if row["causation_ids"] is not None else None
    )
    return LedgerEvent(
        event_id=row["event_id"],
        event_type=row["event_type"],
        envelope_version=row["envelope_version"],
        event_version=row["event_version"],
        actor_id=row["actor_id"],
        actor_type=row["actor_type"],
        integrity_hash=row["integrity_hash"],
        previous_hash=row["previous_hash"],
        payload=payload,
        on_behalf_of=row["on_behalf_of"],
        correlation_id=row["correlation_id"],
        causation_ids=causation_ids,
        context_task_id=row["context_task_id"] or "",
        context_goal_id=row["context_goal_id"] or "",
        context_team_id=row["context_team_id"] or "",
        context_conversation_id=row["context_conversation_id"] or "",
        classification=row["classification"] or "normal",
        extensions=extensions,
        idempotency_key=row["idempotency_key"],
        timestamp=row["timestamp"],
        sequence_number=row["sequence_number"],
    )


async def _stream_query(
    conn: Any, sql: str, params: list[Any]
) -> AsyncIterator[LedgerEvent]:
    if hasattr(conn, "cursor"):
        async for row in conn.cursor(sql, *params):
            yield _row_to_ledger_event(row)
    else:
        rows = await conn.fetch(sql, *params)
        for row in rows:
            yield _row_to_ledger_event(row)


async def read_events(
    conn: Any,
    *,
    event_type: Optional[str] = None,
    context_goal_id: Optional[str] = None,
    correlation_id: Optional[str | uuid.UUID] = None,
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: Optional[int] = None,
) -> AsyncIterator[LedgerEvent]:
    """Stream ledger events oldest first by sequence_number through a server-side cursor."""
    conditions: list[str] = []
    params: list[Any] = []

    if event_type is not None:
        params.append(event_type)
        conditions.append(f"event_type = ${len(params)}")

    if context_goal_id is not None:
        params.append(context_goal_id)
        conditions.append(f"context_goal_id = ${len(params)}")

    if correlation_id is not None:
        corr_val = (
            uuid.UUID(str(correlation_id))
            if not isinstance(correlation_id, uuid.UUID)
            else correlation_id
        )
        params.append(corr_val)
        conditions.append(f"correlation_id = ${len(params)}")

    if since is not None:
        kind, val = await _resolve_bound(conn, since)
        params.append(val)
        col = "sequence_number" if kind == "seq" else "timestamp"
        conditions.append(f"{col} >= ${len(params)}")

    if until is not None:
        kind, val = await _resolve_bound(conn, until)
        params.append(val)
        col = "sequence_number" if kind == "seq" else "timestamp"
        conditions.append(f"{col} <= ${len(params)}")

    sql = (
        "SELECT event_id, event_type, envelope_version, event_version, "
        "timestamp, sequence_number, actor_id, actor_type, on_behalf_of, "
        "correlation_id, causation_ids, context_task_id, context_goal_id, "
        "context_team_id, context_conversation_id, classification, "
        "integrity_hash, previous_hash, payload, extensions, idempotency_key "
        "FROM ledger_events"
    )
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY sequence_number ASC"
    if limit is not None:
        params.append(limit)
        sql += f" LIMIT ${len(params)}"

    in_tx = hasattr(conn, "is_in_transaction") and conn.is_in_transaction()
    if in_tx:
        async for ev in _stream_query(conn, sql, params):
            yield ev
    elif hasattr(conn, "transaction"):
        async with conn.transaction():
            async for ev in _stream_query(conn, sql, params):
                yield ev
    else:
        async for ev in _stream_query(conn, sql, params):
            yield ev


async def verify_chain(
    conn: Any,
    *,
    since: Optional[Any] = None,
    limit: Optional[int] = None,
) -> ChainVerification:
    """Verify hash chain integrity from genesis or a starting point."""
    in_tx = hasattr(conn, "is_in_transaction") and conn.is_in_transaction()
    if in_tx:
        return await _do_verify_chain(conn, since, limit)
    if hasattr(conn, "transaction"):
        async with conn.transaction():
            return await _do_verify_chain(conn, since, limit)
    return await _do_verify_chain(conn, since, limit)


async def _do_verify_chain(
    conn: Any,
    since: Optional[Any],
    limit: Optional[int],
) -> ChainVerification:
    prior = GENESIS
    conditions: list[str] = []
    params: list[Any] = []

    if since is not None:
        kind, val = await _resolve_bound(conn, since)
        if kind == "seq":
            prior_row = await conn.fetchrow(
                "SELECT integrity_hash FROM ledger_events "
                "WHERE sequence_number < $1 ORDER BY sequence_number DESC LIMIT 1",
                val,
            )
            if prior_row is not None:
                prior = prior_row["integrity_hash"]
            params.append(val)
            conditions.append(f"sequence_number >= ${len(params)}")
        else:
            prior_row = await conn.fetchrow(
                "SELECT integrity_hash FROM ledger_events "
                "WHERE timestamp < $1 ORDER BY sequence_number DESC LIMIT 1",
                val,
            )
            if prior_row is not None:
                prior = prior_row["integrity_hash"]
            params.append(val)
            conditions.append(f"timestamp >= ${len(params)}")

    sql = (
        "SELECT event_id, sequence_number, integrity_hash, previous_hash, payload "
        "FROM ledger_events"
    )
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY sequence_number ASC"
    if limit is not None:
        params.append(limit)
        sql += f" LIMIT ${len(params)}"

    if hasattr(conn, "cursor"):
        async for row in conn.cursor(sql, *params):
            event_id = row["event_id"]
            prev_hash = row["previous_hash"]
            integrity_hash = row["integrity_hash"]
            payload = _as_dict(row["payload"])

            if prev_hash != prior:
                return ChainVerification(
                    ok=False,
                    bad_event_id=event_id,
                    expected_hash=prior,
                    actual_hash=prev_hash,
                )

            computed = chain_hash(prev_hash, payload)
            if computed != integrity_hash:
                return ChainVerification(
                    ok=False,
                    bad_event_id=event_id,
                    expected_hash=computed,
                    actual_hash=integrity_hash,
                )

            prior = integrity_hash
        return ChainVerification(ok=True)

    rows = await conn.fetch(sql, *params)
    for row in rows:
        event_id = row["event_id"]
        prev_hash = row["previous_hash"]
        integrity_hash = row["integrity_hash"]
        payload = _as_dict(row["payload"])

        if prev_hash != prior:
            return ChainVerification(
                ok=False,
                bad_event_id=event_id,
                expected_hash=prior,
                actual_hash=prev_hash,
            )

        computed = chain_hash(prev_hash, payload)
        if computed != integrity_hash:
            return ChainVerification(
                ok=False,
                bad_event_id=event_id,
                expected_hash=computed,
                actual_hash=integrity_hash,
            )

        prior = integrity_hash
    return ChainVerification(ok=True)


__all__ = [
    "GLOBAL_CHAIN_LOCK_ID",
    "generate_event_id",
    "append_event",
    "register_pre_append_hook",
    "register_post_append_hook",
    "read_events",
    "verify_chain",
]
