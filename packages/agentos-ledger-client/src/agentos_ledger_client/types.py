"""Core data types and structures for AgentOS State Ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class BlobRef:
    """Content-addressed reference to a stored blob in the ledger.

    Attributes:
        blob_id: Unique identifier of the blob record.
        sha256: Hexadecimal SHA-256 digest of the blob contents.
        size_bytes: Size of the blob payload in bytes.
        content_type: MIME type of the payload (e.g. application/octet-stream).
        storage_backend: Storage mechanism used: 'inline' or 'gcs'.
        created_at: Timestamp when the blob record was created.
        gcs_uri: Google Cloud Storage URI if stored externally, else None.
    """

    blob_id: UUID
    sha256: str
    size_bytes: int
    content_type: str
    storage_backend: str
    created_at: datetime
    gcs_uri: str | None = None

    @property
    def backend(self) -> str:
        """Alias for storage_backend."""
        return self.storage_backend

    def to_dict(self) -> dict[str, Any]:
        """Convert reference to dictionary representation for provenance records."""
        return {
            "blob_id": str(self.blob_id),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "storage_backend": self.storage_backend,
            "backend": self.storage_backend,
            "created_at": self.created_at.isoformat(),
            "gcs_uri": self.gcs_uri,
        }


@dataclass(frozen=True, slots=True)
class ChainHead:
    """Head of the ledger hash chain."""

    id: int
    last_event_id: UUID
    last_hash: str
    event_count: int
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """Immutable record of an event appended to the State Ledger."""

    event_id: UUID
    event_type: str
    envelope_version: int
    event_version: int
    actor_id: UUID
    actor_type: str
    integrity_hash: str
    previous_hash: str
    payload: dict[str, Any]
    on_behalf_of: UUID | None = None
    correlation_id: UUID | None = None
    causation_ids: list[UUID] | None = None
    context_task_id: str = ""
    context_goal_id: str = ""
    context_team_id: str = ""
    context_conversation_id: str = ""
    classification: str = "normal"
    extensions: dict[str, Any] | None = None
    idempotency_key: str | None = None
    timestamp: datetime | None = None
    sequence_number: int | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.timestamp is not None and self.created_at is None:
            object.__setattr__(self, "created_at", self.timestamp)
        elif self.created_at is not None and self.timestamp is None:
            object.__setattr__(self, "timestamp", self.created_at)


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Result of verifying hash integrity of a ledger chain sequence.

    Attributes:
        ok: True if the chain sequence verifies without error, False otherwise.
        bad_event_id: UUID of the first divergent event row, or None on success.
        expected_hash: Hash value that was expected, or None on success.
        actual_hash: Hash value that was actually found/computed, or None on success.
    """

    ok: bool
    bad_event_id: UUID | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Result of an append_event invocation."""

    event_id: str
    integrity_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "integrity_hash": self.integrity_hash,
        }


__all__ = [
    "BlobRef",
    "ChainHead",
    "ChainVerification",
    "LedgerEvent",
    "AppendResult",
]
