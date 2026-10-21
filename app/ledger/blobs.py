"""Content-addressed binary blob storage for the State Ledger.

Re-exports core primitives from agentos_ledger_client.blobs with full backward compatibility.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import agentos_ledger_client.blobs as _core_blobs
from agentos_ledger_client.blobs import (
    INLINE_THRESHOLD_BYTES,
    STORAGE_GCS,
    STORAGE_INLINE,
    BlobRef,
    get_blob_ref,
    get_blob_ref_by_sha256,
)
from app.storage import download_gcs_uri_bytes, get_gcs_client, resource_bucket_name

# Exposed functions for patching / custom calls
_upload_to_gcs = _core_blobs._upload_to_gcs


async def store_blob(
    conn: Any,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> BlobRef:
    """Store content-addressed binary data, selecting inline or GCS storage by size."""
    uploader = globals().get("_upload_to_gcs", _core_blobs._upload_to_gcs)
    return await _core_blobs.store_blob(
        conn,
        data,
        content_type=content_type,
        upload_fn=uploader,
    )


async def read_blob(conn: Any, blob_id: UUID | str) -> bytes:
    """Read binary data for a content-addressed blob by its UUID."""
    downloader = globals().get("download_gcs_uri_bytes", _core_blobs.download_gcs_uri_bytes)
    return await _core_blobs.read_blob(
        conn,
        blob_id,
        download_fn=downloader,
    )


async def read_blob_by_sha256(conn: Any, sha256: str) -> bytes:
    """Read binary data for a content-addressed blob by its SHA-256 hash."""
    ref = await get_blob_ref_by_sha256(conn, sha256)
    if ref is None:
        raise KeyError(f"Blob with sha256 {sha256!r} not found")
    return await read_blob(conn, ref.blob_id)


__all__ = [
    "BlobRef",
    "store_blob",
    "read_blob",
    "get_blob_ref",
    "get_blob_ref_by_sha256",
    "read_blob_by_sha256",
    "INLINE_THRESHOLD_BYTES",
    "STORAGE_INLINE",
    "STORAGE_GCS",
    "_upload_to_gcs",
    "download_gcs_uri_bytes",
    "get_gcs_client",
    "resource_bucket_name",
]
