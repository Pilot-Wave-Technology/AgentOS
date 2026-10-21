from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any
from unittest.mock import AsyncMock
import uuid
from uuid import UUID

import pytest

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


class MockRecord(dict):
    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


class InMemoryBlobsDb:
    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, Any]] = {}
        self.sha256_index: dict[str, UUID] = {}

    async def fetchrow(self, query: str, *args: Any) -> MockRecord | None:
        normalized = " ".join(query.strip().split())

        if "WHERE sha256 = $1" in normalized:
            sha256_val = args[0]
            blob_id = self.sha256_index.get(sha256_val)
            if blob_id is None:
                return None
            return MockRecord(self.rows[blob_id])

        if "WHERE blob_id = $1" in normalized:
            blob_id_val = args[0]
            if isinstance(blob_id_val, str):
                blob_id_val = UUID(blob_id_val)
            row = self.rows.get(blob_id_val)
            if row is None:
                return None
            return MockRecord(row)

        if "INSERT INTO public.ledger_blobs" in normalized:
            sha256_val = args[0]
            size_bytes = args[1]
            content_type = args[2]
            storage_backend = args[3]
            bytea_data = args[4]
            gcs_uri = args[5]

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

        raise NotImplementedError(f"Query not supported in mock: {query}")


@pytest.mark.asyncio
async def test_store_and_read_inline_blob():
    conn = InMemoryBlobsDb()
    data = b"small inline test payload"
    expected_sha = hashlib.sha256(data).hexdigest()

    ref = await store_blob(conn, data, content_type="text/plain")

    assert ref.storage_backend == STORAGE_INLINE
    assert ref.backend == STORAGE_INLINE
    assert ref.sha256 == expected_sha
    assert ref.size_bytes == len(data)
    assert ref.content_type == "text/plain"
    assert ref.gcs_uri is None

    # to_dict verification
    d = ref.to_dict()
    assert d["blob_id"] == str(ref.blob_id)
    assert d["sha256"] == expected_sha

    # Read back by UUID
    retrieved = await read_blob(conn, ref.blob_id)
    assert retrieved == data

    # Read back by string UUID
    assert await read_blob(conn, str(ref.blob_id)) == data

    # Read back by SHA-256
    assert await read_blob_by_sha256(conn, expected_sha) == data


@pytest.mark.asyncio
async def test_store_inline_exact_threshold():
    conn = InMemoryBlobsDb()
    data = b"a" * INLINE_THRESHOLD_BYTES
    ref = await store_blob(conn, data)
    assert ref.storage_backend == STORAGE_INLINE
    assert ref.size_bytes == INLINE_THRESHOLD_BYTES
    retrieved = await read_blob(conn, ref.blob_id)
    assert retrieved == data


@pytest.mark.asyncio
async def test_deduplication():
    conn = InMemoryBlobsDb()
    data = b"duplicate test data"
    ref1 = await store_blob(conn, data)
    ref2 = await store_blob(conn, data)
    assert ref1.blob_id == ref2.blob_id
    assert ref1.sha256 == ref2.sha256
    assert len(conn.rows) == 1


@pytest.mark.asyncio
async def test_store_and_read_gcs_blob():
    conn = InMemoryBlobsDb()
    large_data = b"b" * (INLINE_THRESHOLD_BYTES + 10)
    expected_sha = hashlib.sha256(large_data).hexdigest()
    mock_uri = f"gs://bucket/blobs/{expected_sha}"

    upload_mock = AsyncMock(return_value=mock_uri)
    download_mock = AsyncMock(return_value=large_data)

    ref = await store_blob(conn, large_data, upload_fn=upload_mock)
    assert ref.storage_backend == STORAGE_GCS
    assert ref.gcs_uri == mock_uri
    assert ref.sha256 == expected_sha
    upload_mock.assert_awaited_once()

    retrieved = await read_blob(conn, ref.blob_id, download_fn=download_mock)
    assert retrieved == large_data
    download_mock.assert_awaited_once_with(mock_uri)


@pytest.mark.asyncio
async def test_tamper_integrity_check():
    conn = InMemoryBlobsDb()
    data = b"tamper test"
    ref = await store_blob(conn, data)

    # Tamper with the data in storage
    conn.rows[ref.blob_id]["data"] = b"tampered bytes"

    with pytest.raises(ValueError, match="integrity check failed"):
        await read_blob(conn, ref.blob_id)


@pytest.mark.asyncio
async def test_missing_blob():
    conn = InMemoryBlobsDb()
    with pytest.raises(KeyError):
        await read_blob(conn, uuid.uuid4())
    with pytest.raises(KeyError):
        await read_blob_by_sha256(conn, "0" * 64)
