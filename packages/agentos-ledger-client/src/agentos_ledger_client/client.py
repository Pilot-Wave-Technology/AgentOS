"""High-level async client for the AgentOS State Ledger."""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Optional
from uuid import UUID

from agentos_ledger_client.blobs import (
    get_blob_ref,
    get_blob_ref_by_sha256,
    read_blob,
    read_blob_by_sha256,
    store_blob,
)
from agentos_ledger_client.chain import (
    chain_hash,
    envelope_digest,
)
from agentos_ledger_client.journal import (
    append_event,
    read_events,
    verify_chain,
)
from agentos_ledger_client.types import (
    AppendResult,
    BlobRef,
    ChainHead,
    ChainVerification,
    LedgerEvent,
)


class LedgerClient:
    """Client for appending and reading ledger events and content-addressed blobs.

    Can be initialized with an asyncpg.Connection, asyncpg.Pool, or a connection callable.
    """

    def __init__(self, target: Any = None) -> None:
        self._target = target

    async def _resolve_conn(self, conn: Optional[Any] = None) -> tuple[Any, bool]:
        """Resolve a connection from argument, pool, or target.

        Returns (connection, should_release).
        """
        if conn is not None:
            return conn, False
        if self._target is None:
            raise ValueError("No database connection or pool provided.")
        if hasattr(self._target, "acquire"):
            # asyncpg.Pool
            acquired = await self._target.acquire()
            return acquired, True
        return self._target, False

    async def append(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        actor_id: UUID,
        actor_type: str = "human",
        conn: Optional[Any] = None,
        event_id: Optional[UUID] = None,
        on_behalf_of: Optional[UUID] = None,
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
    ) -> Optional[AppendResult]:
        """Append an event to the ledger within an advisory-locked transaction."""
        c, should_release = await self._resolve_conn(conn)
        try:
            # If connection is not already in a transaction, wrap in transaction
            if hasattr(c, "transaction"):
                async with c.transaction():
                    res = await append_event(
                        c,
                        event_id=event_id,
                        event_type=event_type,
                        payload=payload,
                        actor_id=actor_id,
                        actor_type=actor_type,
                        on_behalf_of=on_behalf_of,
                        envelope_version=envelope_version,
                        event_version=event_version,
                        correlation_id=correlation_id,
                        causation_ids=causation_ids,
                        context_task_id=context_task_id,
                        context_goal_id=context_goal_id,
                        context_team_id=context_team_id,
                        context_conversation_id=context_conversation_id,
                        classification=classification,
                        extensions=extensions,
                        idempotency_key=idempotency_key,
                    )
            else:
                res = await append_event(
                    c,
                    event_id=event_id,
                    event_type=event_type,
                    payload=payload,
                    actor_id=actor_id,
                    actor_type=actor_type,
                    on_behalf_of=on_behalf_of,
                    envelope_version=envelope_version,
                    event_version=event_version,
                    correlation_id=correlation_id,
                    causation_ids=causation_ids,
                    context_task_id=context_task_id,
                    context_goal_id=context_goal_id,
                    context_team_id=context_team_id,
                    context_conversation_id=context_conversation_id,
                    classification=classification,
                    extensions=extensions,
                    idempotency_key=idempotency_key,
                )
            if res is None:
                return None
            return AppendResult(
                event_id=res["event_id"],
                integrity_hash=res["integrity_hash"],
            )
        finally:
            if should_release:
                await self._target.release(c)

    async def store_blob(
        self,
        data: bytes,
        content_type: str = "application/octet-stream",
        *,
        conn: Optional[Any] = None,
        upload_fn: Optional[Callable[..., Any]] = None,
    ) -> BlobRef:
        """Store binary payload inline or in GCS."""
        c, should_release = await self._resolve_conn(conn)
        try:
            return await store_blob(
                c, data, content_type=content_type, upload_fn=upload_fn
            )
        finally:
            if should_release:
                await self._target.release(c)

    async def read_blob(
        self,
        blob_id: UUID | str,
        *,
        conn: Optional[Any] = None,
        download_fn: Optional[Callable[..., Any]] = None,
    ) -> bytes:
        """Read binary payload by UUID."""
        c, should_release = await self._resolve_conn(conn)
        try:
            return await read_blob(c, blob_id, download_fn=download_fn)
        finally:
            if should_release:
                await self._target.release(c)

    async def get_blob_ref(
        self,
        blob_id: UUID | str,
        *,
        conn: Optional[Any] = None,
    ) -> BlobRef | None:
        """Get BlobRef metadata by UUID."""
        c, should_release = await self._resolve_conn(conn)
        try:
            return await get_blob_ref(c, blob_id)
        finally:
            if should_release:
                await self._target.release(c)

    async def get_blob_ref_by_sha256(
        self,
        sha256: str,
        *,
        conn: Optional[Any] = None,
    ) -> BlobRef | None:
        """Get BlobRef metadata by SHA-256."""
        c, should_release = await self._resolve_conn(conn)
        try:
            return await get_blob_ref_by_sha256(c, sha256)
        finally:
            if should_release:
                await self._target.release(c)

    async def read_blob_by_sha256(
        self,
        sha256: str,
        *,
        conn: Optional[Any] = None,
        download_fn: Optional[Callable[..., Any]] = None,
    ) -> bytes:
        """Read binary payload by SHA-256."""
        c, should_release = await self._resolve_conn(conn)
        try:
            return await read_blob_by_sha256(c, sha256, download_fn=download_fn)
        finally:
            if should_release:
                await self._target.release(c)

    async def get_chain_head(self, *, conn: Optional[Any] = None) -> ChainHead | None:
        """Retrieve current chain head record."""
        c, should_release = await self._resolve_conn(conn)
        try:
            row = await c.fetchrow(
                "SELECT id, last_event_id, last_hash, event_count, updated_at FROM chain_head WHERE id = 1"
            )
            if row is None:
                return None
            return ChainHead(
                id=row["id"],
                last_event_id=row["last_event_id"],
                last_hash=row["last_hash"],
                event_count=row["event_count"],
                updated_at=row.get("updated_at"),
            )
        finally:
            if should_release:
                await self._target.release(c)

    async def read_events(
        self,
        *,
        conn: Optional[Any] = None,
        event_type: Optional[str] = None,
        context_goal_id: Optional[str] = None,
        correlation_id: Optional[str | UUID] = None,
        since: Optional[Any] = None,
        until: Optional[Any] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[LedgerEvent]:
        """Stream ledger events oldest first by sequence_number."""
        c, should_release = await self._resolve_conn(conn)
        try:
            async for ev in read_events(
                c,
                event_type=event_type,
                context_goal_id=context_goal_id,
                correlation_id=correlation_id,
                since=since,
                until=until,
                limit=limit,
            ):
                yield ev
        finally:
            if should_release:
                await self._target.release(c)

    async def verify_chain(
        self,
        *,
        conn: Optional[Any] = None,
        since: Optional[Any] = None,
        limit: Optional[int] = None,
    ) -> ChainVerification:
        """Verify hash chain integrity."""
        c, should_release = await self._resolve_conn(conn)
        try:
            return await verify_chain(c, since=since, limit=limit)
        finally:
            if should_release:
                await self._target.release(c)

    @staticmethod
    def compute_chain_hash(previous_hash: str, payload: Any) -> str:
        """Compute SHA-256 hash chaining formula."""
        return chain_hash(previous_hash, payload)

    @staticmethod
    def compute_envelope_digest(
        *,
        event_type: str,
        actor_id: Any,
        actor_type: str,
        on_behalf_of: Any = None,
        payload: Any = None,
    ) -> str:
        """Compute attribution envelope digest."""
        return envelope_digest(
            event_type=event_type,
            actor_id=actor_id,
            actor_type=actor_type,
            on_behalf_of=on_behalf_of,
            payload=payload,
        )


__all__ = ["LedgerClient"]
