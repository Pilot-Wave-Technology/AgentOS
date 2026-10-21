"""Content-addressed binary blob storage for the AgentOS State Ledger.

Provides storage and retrieval of arbitrary binary payloads with automatic tiering:
  - Payloads <= 1MB: stored inline as BYTEA in public.ledger_blobs
  - Payloads > 1MB: uploaded to Google Cloud Storage (GCS) and referenced by URI

All payloads are content-addressed by SHA-256 hex digest, providing automatic
deduplication and tamper-evident provenance tracking across the lifecycle.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import os
from typing import Any, Callable, Final
from uuid import UUID

from agentos_ledger_client.integration import current
from agentos_ledger_client.types import BlobRef

INLINE_THRESHOLD_BYTES: Final[int] = 1024 * 1024  # 1 MB threshold: <=1MB inline, >1MB GCS
STORAGE_INLINE: Final[str] = "inline"
STORAGE_GCS: Final[str] = "gcs"

_gcs_client: Any = None


def resource_bucket_name() -> str:
    """Shared bucket for ledger blobs."""
    # NO HARDCODED FALLBACK, and no `except` around the lookup.
    #
    # This used to end `os.environ.get("GCS_RESOURCE_BUCKET",
    # "${RESOURCE_BUCKET}")` inside a bare `except Exception`, so a
    # CONFIGURATION FAILURE did not raise -- it silently redirected every
    # ledger blob to a hardcoded bucket. A misconfigured deployment wrote
    # somewhere nobody was looking and said nothing. The name also broke
    # the no-`pwh` convention and the OSS rule on hardcoded project names.
    #
    # FROM THE HOST, NOT FROM AN IMPORT. `integration.configure()` is
    # how agentos hands its settings in; a consumer supplies its own
    # or none. See integration.py for why importing `app.*` here was
    # worse than useless.
    name = current().bucket_name or os.environ.get("GCS_RESOURCE_BUCKET", "")
    if not name:
        raise RuntimeError(
            "no GCS bucket configured for ledger blobs: set "
            "GCS_RESOURCE_BUCKET. Blobs of 1MB or less are stored inline "
            "and need no bucket at all, so this is only reached by a "
            "payload over the inline threshold.")
    return name


def get_gcs_client() -> Any:
    """Get or initialize GCS client if google-cloud-storage is installed."""
    factory = current().gcs_client
    if factory is not None:
        return factory()
    try:
        from google.cloud import storage as gcs
        global _gcs_client
        if _gcs_client is None:
            _gcs_client = gcs.Client()
        return _gcs_client
    except Exception as exc:
        raise RuntimeError(
            "GCS client is not available; install google-cloud-storage or provide upload_fn/download_fn"
        ) from exc


async def _upload_to_gcs(
    bucket_name: str,
    blob_path: str,
    data: bytes,
    content_type: str,
) -> str:
    """Upload payload to GCS in threadpool executor and return gs:// URI."""
    client = get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: blob.upload_from_string(
            data, content_type=content_type or "application/octet-stream"
        ),
    )
    return f"gs://{bucket_name}/{blob_path}"


async def download_gcs_uri_bytes(gcs_uri: str) -> bytes:
    """Download raw bytes from an arbitrary gs:// URI."""
    if not gcs_uri or not gcs_uri.startswith("gs://"):
        raise ValueError(f"not a GCS URI: {gcs_uri!r}")
    without_scheme = gcs_uri[5:]
    bucket_name, _, blob_path = without_scheme.partition("/")
    if not blob_path:
        raise ValueError(f"missing blob path in URI: {gcs_uri!r}")

    host = current().download
    if host is not None:
        return await host(gcs_uri)

    def _download() -> bytes:
        client = get_gcs_client()
        return client.bucket(bucket_name).blob(blob_path).download_as_bytes()

    return await asyncio.get_running_loop().run_in_executor(None, _download)


async def store_blob(
    conn: Any,
    data: bytes,
    content_type: str = "application/octet-stream",
    *,
    upload_fn: Callable[..., Any] | None = None,
) -> BlobRef:
    """Store content-addressed binary data, selecting inline or GCS storage by size.

    Deduplicates writes based on SHA-256 hash: if the blob already exists,
    returns the existing BlobRef without re-uploading or duplicating records.

    Args:
        conn: asyncpg connection or compatible database interface.
        data: Raw payload bytes to store.
        content_type: MIME content type of payload.
        upload_fn: Optional callable for GCS upload (for test patching/custom backends).

    Returns:
        BlobRef referencing the persisted payload.

    Raises:
        TypeError: If data is not bytes or bytearray.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"data must be bytes-like, got {type(data).__name__}")
    payload = bytes(data)
    size_bytes = len(payload)
    sha256_hex = hashlib.sha256(payload).hexdigest()
    effective_content_type = content_type or "application/octet-stream"

    # Deduplication check: return existing record if already present
    existing = await conn.fetchrow(
        """
        SELECT blob_id, sha256, size_bytes, content_type, storage_backend, gcs_uri, created_at
        FROM public.ledger_blobs
        WHERE sha256 = $1
        """,
        sha256_hex,
    )
    if existing is not None:
        return BlobRef(
            blob_id=existing["blob_id"],
            sha256=existing["sha256"],
            size_bytes=existing["size_bytes"],
            content_type=existing["content_type"],
            storage_backend=existing["storage_backend"],
            created_at=existing["created_at"],
            gcs_uri=existing["gcs_uri"],
        )

    # Determine storage tier: inline if <= 1MB, GCS if > 1MB
    if size_bytes <= INLINE_THRESHOLD_BYTES:
        storage_backend = STORAGE_INLINE
        gcs_uri = None
        bytea_data = payload
    else:
        storage_backend = STORAGE_GCS
        bytea_data = None
        # THE BUCKET IS ALWAYS RESOLVED, even when the caller supplies
        # its own `upload_fn`.
        #
        # I briefly skipped it in that case, reasoning that an uploader
        # which returns its own URI does not need one. That is true of a
        # test passing a mock and false of the host: `app/ledger/blobs.py`
        # ALWAYS passes `upload_fn=` (so a patched `_upload_to_gcs` takes
        # effect), so "caller supplied an uploader" is not a signal about
        # the caller at all, and the optimisation silently handed every
        # production upload an empty bucket name.
        #
        # The uploader receives the bucket as an argument and may ignore
        # it; what it must not do is receive the wrong one.
        bucket_name = resource_bucket_name()
        uploader = upload_fn or _upload_to_gcs
        blob_path = f"blobs/{sha256_hex}"
        gcs_uri = await uploader(
            bucket_name=bucket_name,
            blob_path=blob_path,
            data=payload,
            content_type=effective_content_type,
        )

    row = await conn.fetchrow(
        """
        INSERT INTO public.ledger_blobs (
            sha256, size_bytes, content_type, storage_backend, data, gcs_uri
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (sha256) DO UPDATE
            SET sha256 = EXCLUDED.sha256
        RETURNING blob_id, sha256, size_bytes, content_type, storage_backend, gcs_uri, created_at
        """,
        sha256_hex,
        size_bytes,
        effective_content_type,
        storage_backend,
        bytea_data,
        gcs_uri,
    )

    return BlobRef(
        blob_id=row["blob_id"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
        storage_backend=row["storage_backend"],
        created_at=row["created_at"],
        gcs_uri=row["gcs_uri"],
    )


async def read_blob(
    conn: Any,
    blob_id: UUID | str,
    *,
    download_fn: Callable[..., Any] | None = None,
) -> bytes:
    """Read binary data for a content-addressed blob by its UUID.

    Fetches from inline BYTEA or GCS depending on storage_backend, and verifies
    payload integrity against the stored SHA-256 hash.

    Args:
        conn: asyncpg connection or compatible database interface.
        blob_id: UUID of blob to read.
        download_fn: Optional callable for GCS download.

    Returns:
        Raw bytes of the retrieved blob.

    Raises:
        KeyError: If blob_id not found in ledger_blobs.
        ValueError: If SHA-256 integrity check fails, payload missing, or backend unknown.
    """
    if isinstance(blob_id, str):
        blob_id = UUID(blob_id)

    row = await conn.fetchrow(
        """
        SELECT blob_id, sha256, size_bytes, content_type, storage_backend, data, gcs_uri
        FROM public.ledger_blobs
        WHERE blob_id = $1
        """,
        blob_id,
    )
    if row is None:
        raise KeyError(f"Blob not found: {blob_id}")

    backend = row["storage_backend"]
    if backend == STORAGE_INLINE:
        raw_data = row["data"]
        if raw_data is None:
            raise ValueError(f"Corrupted inline blob {blob_id}: data payload is NULL")
        payload = bytes(raw_data)
    elif backend == STORAGE_GCS:
        gcs_uri = row["gcs_uri"]
        if not gcs_uri:
            raise ValueError(f"Corrupted GCS blob {blob_id}: gcs_uri is NULL")
        downloader = download_fn or download_gcs_uri_bytes
        payload = await downloader(gcs_uri)
    else:
        raise ValueError(f"Unsupported storage backend: {backend!r}")

    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != row["sha256"]:
        raise ValueError(
            f"Blob integrity check failed for {blob_id}: "
            f"expected SHA-256 {row['sha256']}, got {actual_sha256}"
        )

    return payload


async def get_blob_ref(conn: Any, blob_id: UUID | str) -> BlobRef | None:
    """Fetch BlobRef metadata by blob_id without loading payload data."""
    if isinstance(blob_id, str):
        blob_id = UUID(blob_id)
    row = await conn.fetchrow(
        """
        SELECT blob_id, sha256, size_bytes, content_type, storage_backend, gcs_uri, created_at
        FROM public.ledger_blobs
        WHERE blob_id = $1
        """,
        blob_id,
    )
    if row is None:
        return None
    return BlobRef(
        blob_id=row["blob_id"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
        storage_backend=row["storage_backend"],
        created_at=row["created_at"],
        gcs_uri=row["gcs_uri"],
    )


async def get_blob_ref_by_sha256(conn: Any, sha256: str) -> BlobRef | None:
    """Fetch BlobRef metadata by sha256 without loading payload data."""
    row = await conn.fetchrow(
        """
        SELECT blob_id, sha256, size_bytes, content_type, storage_backend, gcs_uri, created_at
        FROM public.ledger_blobs
        WHERE sha256 = $1
        """,
        sha256,
    )
    if row is None:
        return None
    return BlobRef(
        blob_id=row["blob_id"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
        storage_backend=row["storage_backend"],
        created_at=row["created_at"],
        gcs_uri=row["gcs_uri"],
    )


async def read_blob_by_sha256(
    conn: Any,
    sha256: str,
    *,
    download_fn: Callable[..., Any] | None = None,
) -> bytes:
    """Read binary data for a content-addressed blob by its SHA-256 hash."""
    ref = await get_blob_ref_by_sha256(conn, sha256)
    if ref is None:
        raise KeyError(f"Blob with sha256 {sha256!r} not found")
    return await read_blob(conn, ref.blob_id, download_fn=download_fn)


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
    "resource_bucket_name",
    "get_gcs_client",
    "_upload_to_gcs",
    "download_gcs_uri_bytes",
]
