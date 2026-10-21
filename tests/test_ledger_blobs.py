"""Unit tests for State Ledger blob storage and RL event schema.

Tests cover:
  1. MARL schema constants in app.ledger.schema and app.ledger exports.
  2. BlobRef dataclass immutability, fields, properties, and dictionary representation.
  3. Migration DDL syntax, constraints, and SEQUENCE registration.
  4. Content-addressed storage for inline blobs (<= 1MB) with sha256 deduplication.
  5. Content-addressed storage for GCS blobs (> 1MB) with external URI tracking.
  6. Read blob round-trip retrieval for both inline and GCS tiers.
  7. Tamper-evident integrity verification (hash mismatch raises ValueError).
  8. Boundary conditions: exact 1MB inline threshold, missing blobs, invalid inputs.
  9. Provenance tracking: preserves sha256, size_bytes, content_type, timestamps.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
import uuid
from uuid import UUID

import pytest

from app.ledger import BlobRef, read_blob, store_blob
from app.ledger.blobs import (
    INLINE_THRESHOLD_BYTES,
    STORAGE_GCS,
    STORAGE_INLINE,
    get_blob_ref,
    get_blob_ref_by_sha256,
    read_blob_by_sha256,
)
from app.services.tests.shipped_tree import (
    IS_DISTRIBUTION,
    baseline_mentions,
    create_table,
)
from app.ledger.schema import (
    MARL_ACTION_EXECUTED,
    MARL_ACTION_PROPOSED,
    MARL_BELIEF_UPDATED,
    MARL_MODEL_CHECKPOINT,
    MARL_OBSERVATION,
    MARL_OUTCOME_OBSERVED,
    MARL_SIM_TICK,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


# ── Mock Asyncpg Connection for Unit Tests ───────────────────────────────────

class MockRecord(dict):
    """Dict-like record emulating asyncpg.Record access."""

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


class InMemoryLedgerBlobsConnection:
    """In-memory database stub simulating asyncpg operations on public.ledger_blobs."""

    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, Any]] = {}
        self.sha256_index: dict[str, UUID] = {}

    async def fetchrow(self, query: str, *args: Any) -> MockRecord | None:
        normalized_query = " ".join(query.strip().split())

        # SELECT WHERE sha256 = $1
        if "WHERE sha256 = $1" in normalized_query:
            sha256_val = args[0]
            blob_id = self.sha256_index.get(sha256_val)
            if blob_id is None:
                return None
            return MockRecord(self.rows[blob_id])

        # SELECT WHERE blob_id = $1
        if "WHERE blob_id = $1" in normalized_query:
            blob_id_val = args[0]
            if isinstance(blob_id_val, str):
                blob_id_val = UUID(blob_id_val)
            row = self.rows.get(blob_id_val)
            if row is None:
                return None
            return MockRecord(row)

        # INSERT INTO public.ledger_blobs
        if "INSERT INTO public.ledger_blobs" in normalized_query:
            sha256_val = args[0]
            size_bytes = args[1]
            content_type = args[2]
            storage_backend = args[3]
            bytea_data = args[4]
            gcs_uri = args[5]

            # ON CONFLICT (sha256) DO UPDATE ...
            if sha256_val in self.sha256_index:
                existing_id = self.sha256_index[sha256_val]
                return MockRecord(self.rows[existing_id])

            new_id = uuid.uuid4()
            now_ts = datetime.now(timezone.utc)
            record = {
                "blob_id": new_id,
                "sha256": sha256_val,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "storage_backend": storage_backend,
                "data": bytea_data,
                "gcs_uri": gcs_uri,
                "created_at": now_ts,
            }
            self.rows[new_id] = record
            self.sha256_index[sha256_val] = new_id
            return MockRecord(record)

        raise NotImplementedError(f"Unhandled query in stub: {query}")


# ── 1. RL Event Schema Constants Tests ───────────────────────────────────────

def test_rlagt_schema_constants_values():
    """Verify all 7 MARL event constants match required dot-notation strings."""
    assert MARL_SIM_TICK == "rlagt.sim.tick"
    assert MARL_OBSERVATION == "rlagt.observation"
    assert MARL_BELIEF_UPDATED == "rlagt.belief.updated"
    assert MARL_ACTION_PROPOSED == "rlagt.action.proposed"
    assert MARL_ACTION_EXECUTED == "rlagt.action.executed"
    assert MARL_OUTCOME_OBSERVED == "rlagt.outcome.observed"
    assert MARL_MODEL_CHECKPOINT == "rlagt.model.checkpoint"


def test_app_ledger_exports():
    """Verify app.ledger exports BlobRef, store_blob, and read_blob in public API."""
    import app.ledger as ledger_pkg

    assert hasattr(ledger_pkg, "BlobRef")
    assert hasattr(ledger_pkg, "store_blob")
    assert hasattr(ledger_pkg, "read_blob")
    assert "BlobRef" in ledger_pkg.__all__
    assert "store_blob" in ledger_pkg.__all__
    assert "read_blob" in ledger_pkg.__all__


# ── 2. BlobRef Data Structure & Provenance Tests ─────────────────────────────

def test_blob_ref_immutability_and_slots():
    """Verify BlobRef is a frozen dataclass with slots for provenance immutability."""
    now = datetime.now(timezone.utc)
    ref = BlobRef(
        blob_id=uuid.uuid4(),
        sha256="abc123hash",
        size_bytes=100,
        content_type="text/plain",
        storage_backend="inline",
        created_at=now,
        gcs_uri=None,
    )
    with pytest.raises(FrozenInstanceError):
        ref.size_bytes = 200  # type: ignore[misc]

    assert ref.backend == "inline"
    d = ref.to_dict()
    assert d["sha256"] == "abc123hash"
    assert d["size_bytes"] == 100
    assert d["content_type"] == "text/plain"
    assert d["storage_backend"] == "inline"
    assert d["backend"] == "inline"
    assert d["created_at"] == now.isoformat()
    assert d["gcs_uri"] is None


# ── 3. Migration File & SEQUENCE Validation ──────────────────────────────────

def test_migration_file_exists_and_contains_table_definition():
    """Verify ledger_blobs.sql creates public.ledger_blobs with specified columns and constraints."""
    if IS_DISTRIBUTION:
        # The same columns and constraints, as pg_dump writes them.
        body = create_table("ledger_blobs")
        for col in ("blob_id uuid DEFAULT gen_random_uuid() NOT NULL",
                    "sha256 text NOT NULL",
                    "size_bytes bigint NOT NULL",
                    "content_type text NOT NULL",
                    "storage_backend text NOT NULL",
                    "data bytea",
                    "gcs_uri text",
                    "created_at timestamp with time zone DEFAULT now() NOT NULL"):
            assert col in body, f"ledger_blobs lacks {col!r}"
        assert "CHECK ((storage_backend = ANY (ARRAY['inline'::text, 'gcs'::text])))" in body
        assert baseline_mentions(r"ADD CONSTRAINT ledger_blobs_pkey PRIMARY KEY \(blob_id\)")
        assert baseline_mentions(r"ADD CONSTRAINT ledger_blobs_sha256_key UNIQUE \(sha256\)")
        return
    migration_file = _MIGRATIONS_DIR / "ledger_blobs.sql"
    assert migration_file.exists(), "ledger_blobs.sql migration file must exist"
    content = migration_file.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS public.ledger_blobs" in content
    assert "blob_id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in content
    assert "sha256 TEXT NOT NULL UNIQUE" in content
    assert "size_bytes BIGINT NOT NULL" in content
    assert "content_type TEXT NOT NULL" in content
    assert "storage_backend TEXT NOT NULL CHECK (storage_backend IN ('inline', 'gcs'))" in content
    assert "data BYTEA" in content
    assert "gcs_uri TEXT" in content
    assert "created_at TIMESTAMPTZ NOT NULL DEFAULT now()" in content


def test_migration_sequence_contains_ledger_blobs():
    """Verify migrations/SEQUENCE registers ledger_blobs."""
    if IS_DISTRIBUTION:
        # Squashed: registration means the table is in the shipped schema.
        assert create_table("ledger_blobs")
        return
    sequence_file = _MIGRATIONS_DIR / "SEQUENCE"
    assert sequence_file.exists(), "migrations/SEQUENCE file must exist"
    lines = [
        line.strip()
        for line in sequence_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "ledger_blobs" in lines, "ledger_blobs is not registered"
    # REGISTERED AND DECLARED ONCE -- not "last".
    #
    # This asserted `lines[-1] == "ledger_blobs"`, which is not a
    # property of this migration: it creates an independent table and
    # nothing orders against it. What it actually did was make the
    # ledger suite fail for whoever added the NEXT migration, whatever
    # it was -- caught on the first one (`publish_failures_park_rather
    # _than_loop`, merged from `next`). `migrations/check_sequence.sh`
    # is what owns order and completeness; this owns registration.
    assert lines.count("ledger_blobs") == 1, "declared more than once"


# ── 4. Inline Storage & Round-Trip Tests (<= 1MB) ─────────────────────────────

@pytest.mark.asyncio
async def test_store_and_read_inline_blob_small():
    """Verify store_blob stores small payload <= 1MB inline and read_blob retrieves it."""
    conn = InMemoryLedgerBlobsConnection()
    test_data = b"taximeter-observation-raw-bytes-42"
    content_type = "application/octet-stream"

    ref = await store_blob(conn, test_data, content_type=content_type)

    assert isinstance(ref.blob_id, UUID)
    assert ref.sha256 == hashlib.sha256(test_data).hexdigest()
    assert ref.size_bytes == len(test_data)
    assert ref.content_type == content_type
    assert ref.storage_backend == STORAGE_INLINE
    assert ref.backend == STORAGE_INLINE
    assert ref.gcs_uri is None
    assert isinstance(ref.created_at, datetime)

    # Read back by UUID
    retrieved = await read_blob(conn, ref.blob_id)
    assert retrieved == test_data

    # Read back by string UUID
    retrieved_str_id = await read_blob(conn, str(ref.blob_id))
    assert retrieved_str_id == test_data

    # Read back by sha256
    retrieved_by_sha = await read_blob_by_sha256(conn, ref.sha256)
    assert retrieved_by_sha == test_data


@pytest.mark.asyncio
async def test_store_inline_exact_threshold():
    """Verify exactly 1MB (1048576 bytes) is stored inline."""
    conn = InMemoryLedgerBlobsConnection()
    data_1mb = b"x" * INLINE_THRESHOLD_BYTES

    ref = await store_blob(conn, data_1mb, content_type="application/octet-stream")

    assert ref.storage_backend == STORAGE_INLINE
    assert ref.size_bytes == INLINE_THRESHOLD_BYTES
    assert ref.gcs_uri is None

    retrieved = await read_blob(conn, ref.blob_id)
    assert len(retrieved) == INLINE_THRESHOLD_BYTES
    assert retrieved == data_1mb


@pytest.mark.asyncio
async def test_deduplication_inline_blobs():
    """Verify identical inline payloads return the same BlobRef without duplicate inserts."""
    conn = InMemoryLedgerBlobsConnection()
    data = b"identical-observation-action-packet"

    ref1 = await store_blob(conn, data, content_type="application/json")
    ref2 = await store_blob(conn, data, content_type="application/json")

    assert ref1.blob_id == ref2.blob_id
    assert ref1.sha256 == ref2.sha256
    assert ref1.size_bytes == ref2.size_bytes
    assert len(conn.rows) == 1
    assert len(conn.sha256_index) == 1


# ── 5. GCS External Storage & Round-Trip Tests (> 1MB) ───────────────────────

# Any name. The point is that it comes from configuration rather than
# from a literal buried in the library.
_TEST_BUCKET = "test-ledger-blobs"


@pytest.mark.asyncio
async def test_store_and_read_gcs_blob_large():
    """Verify store_blob routes payloads > 1MB to GCS and read_blob downloads from GCS."""
    conn = InMemoryLedgerBlobsConnection()
    data_large = b"y" * (INLINE_THRESHOLD_BYTES + 1)
    sha256_large = hashlib.sha256(data_large).hexdigest()

    mock_uploaded_uri = f"gs://test-bucket/blobs/{sha256_large}"

    # THE BUCKET IS CONFIGURATION NOW, NOT A LITERAL IN THE LIBRARY.
    #
    # This test asserted `bucket_name="${RESOURCE_BUCKET}"` — the value
    # `resource_bucket_name()` returned from a hardcoded fallback inside
    # a bare `except Exception`. That fallback meant a CONFIGURATION
    # failure silently redirected ledger blobs rather than raising, and
    # this assertion certified the behaviour. The fallback is gone: the
    # bucket comes from the host via `integration.configure()` or from
    # GCS_RESOURCE_BUCKET, and a missing one raises at the call that
    # needs it.
    #
    # So the test supplies one. What it is actually about — that the
    # uploader receives the right path, bytes and content type — is
    # unchanged; only the source of the bucket name is.
    import agentos_ledger_client.integration as _integration
    _integration.configure(bucket_name=_TEST_BUCKET)
    try:
        with patch(
            "app.ledger.blobs._upload_to_gcs",
            new=AsyncMock(return_value=mock_uploaded_uri),
        ) as mock_upload, patch(
            "app.ledger.blobs.download_gcs_uri_bytes",
            new=AsyncMock(return_value=data_large),
        ) as mock_download:
            ref = await store_blob(conn, data_large, content_type="application/octet-stream")

            assert ref.storage_backend == STORAGE_GCS
            assert ref.backend == STORAGE_GCS
            assert ref.size_bytes == len(data_large)
            assert ref.sha256 == sha256_large
            assert ref.gcs_uri == mock_uploaded_uri

            mock_upload.assert_awaited_once_with(
                bucket_name=_TEST_BUCKET,
                blob_path=f"blobs/{sha256_large}",
                data=data_large,
                content_type="application/octet-stream",
            )

            # In-memory row should have data=None and gcs_uri set
            db_row = conn.rows[ref.blob_id]
            assert db_row["data"] is None
            assert db_row["gcs_uri"] == mock_uploaded_uri

            # Read back blob
            retrieved = await read_blob(conn, ref.blob_id)
            assert retrieved == data_large
            mock_download.assert_awaited_once_with(mock_uploaded_uri)
    finally:
        _integration.reset()


@pytest.mark.asyncio
async def test_deduplication_gcs_blobs():
    """Verify identical >1MB payloads deduplicate and do not re-upload to GCS."""
    conn = InMemoryLedgerBlobsConnection()
    data_large = b"z" * (INLINE_THRESHOLD_BYTES + 500)
    sha256_large = hashlib.sha256(data_large).hexdigest()
    mock_uploaded_uri = f"gs://test-bucket/blobs/{sha256_large}"

    with patch(
        "app.ledger.blobs._upload_to_gcs",
        new=AsyncMock(return_value=mock_uploaded_uri),
    ) as mock_upload:
        ref1 = await store_blob(conn, data_large)
        ref2 = await store_blob(conn, data_large)

        assert ref1.blob_id == ref2.blob_id
        assert ref1.sha256 == ref2.sha256
        assert len(conn.rows) == 1
        # GCS upload should be called only once on initial store
        assert mock_upload.await_count == 1


# ── 6. Integrity Verification & Failure Modes ────────────────────────────────

@pytest.mark.asyncio
async def test_integrity_check_detects_corrupted_inline_blob():
    """Verify read_blob raises ValueError when retrieved data does not match stored sha256."""
    conn = InMemoryLedgerBlobsConnection()
    original_data = b"correct-data"
    ref = await store_blob(conn, original_data)

    # Tamper with the raw data in the database
    conn.rows[ref.blob_id]["data"] = b"tampered-data"

    with pytest.raises(ValueError, match="Blob integrity check failed"):
        await read_blob(conn, ref.blob_id)


@pytest.mark.asyncio
async def test_integrity_check_detects_corrupted_gcs_blob():
    """Verify read_blob raises ValueError when downloaded GCS bytes differ from expected hash."""
    conn = InMemoryLedgerBlobsConnection()
    data_large = b"w" * (INLINE_THRESHOLD_BYTES + 10)
    sha256_large = hashlib.sha256(data_large).hexdigest()
    mock_uploaded_uri = f"gs://test-bucket/blobs/{sha256_large}"

    with patch(
        "app.ledger.blobs._upload_to_gcs",
        new=AsyncMock(return_value=mock_uploaded_uri),
    ):
        ref = await store_blob(conn, data_large)

    # Mock GCS returning corrupted payload on read
    with patch(
        "app.ledger.blobs.download_gcs_uri_bytes",
        new=AsyncMock(return_value=b"corrupted-downloaded-bytes"),
    ):
        with pytest.raises(ValueError, match="Blob integrity check failed"):
            await read_blob(conn, ref.blob_id)


@pytest.mark.asyncio
async def test_read_blob_not_found():
    """Verify read_blob raises KeyError for missing blob_id."""
    conn = InMemoryLedgerBlobsConnection()
    random_id = uuid.uuid4()

    with pytest.raises(KeyError, match=f"Blob not found: {random_id}"):
        await read_blob(conn, random_id)


@pytest.mark.asyncio
async def test_store_blob_type_validation():
    """Verify store_blob rejects non-bytes payloads with TypeError."""
    conn = InMemoryLedgerBlobsConnection()

    with pytest.raises(TypeError, match="data must be bytes-like"):
        await store_blob(conn, "string-not-bytes")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_corrupted_backend_records():
    """Verify explicit error handling when row contains invalid backend or null data."""
    conn = InMemoryLedgerBlobsConnection()
    blob_id = uuid.uuid4()
    sha256_val = hashlib.sha256(b"foo").hexdigest()

    # Case 1: inline with None data
    conn.rows[blob_id] = {
        "blob_id": blob_id,
        "sha256": sha256_val,
        "size_bytes": 3,
        "content_type": "text/plain",
        "storage_backend": "inline",
        "data": None,
        "gcs_uri": None,
        "created_at": datetime.now(timezone.utc),
    }
    with pytest.raises(ValueError, match="Corrupted inline blob"):
        await read_blob(conn, blob_id)

    # Case 2: gcs with None gcs_uri
    conn.rows[blob_id]["storage_backend"] = "gcs"
    conn.rows[blob_id]["gcs_uri"] = None
    with pytest.raises(ValueError, match="Corrupted GCS blob"):
        await read_blob(conn, blob_id)

    # Case 3: unknown backend
    conn.rows[blob_id]["storage_backend"] = "s3"
    with pytest.raises(ValueError, match="Unsupported storage backend"):
        await read_blob(conn, blob_id)


# ── 7. Metadata Query Helpers ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_blob_ref_helpers():
    """Verify get_blob_ref and get_blob_ref_by_sha256 retrieve metadata without data."""
    conn = InMemoryLedgerBlobsConnection()
    data = b"provenance-metadata-test"
    ref = await store_blob(conn, data, content_type="application/json")

    # By UUID
    ref_by_id = await get_blob_ref(conn, ref.blob_id)
    assert ref_by_id is not None
    assert ref_by_id.blob_id == ref.blob_id
    assert ref_by_id.sha256 == ref.sha256

    # By SHA-256
    ref_by_sha = await get_blob_ref_by_sha256(conn, ref.sha256)
    assert ref_by_sha is not None
    assert ref_by_sha.blob_id == ref.blob_id
    assert ref_by_sha.sha256 == ref.sha256

    # Not found
    assert await get_blob_ref(conn, uuid.uuid4()) is None
    assert await get_blob_ref_by_sha256(conn, "0000000000000000000000000000000000000000000000000000000000000000") is None
