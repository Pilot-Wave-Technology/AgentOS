"""GCS helpers for agent workspace files."""
import asyncio
import datetime
import logging

from google.cloud import storage as gcs
from app.config import get_settings

_log = logging.getLogger(__name__)

_client: gcs.Client | None = None


def get_gcs_client() -> gcs.Client:
    global _client
    if _client is None:
        _client = gcs.Client()
    return _client


def workspace_bucket_name(pid: str) -> str:
    # GCS bucket names must be all-lowercase; lowercase the goal-id suffix
    # so uppercase goal IDs (e.g. BMIWP) map to a valid bucket name.
    prefix = get_settings().gcs_workspace_bucket_prefix
    return f"{prefix}-{pid.lower()}"


async def upload_workspace_file(pid: str, agent_id: str, filename: str, data: bytes) -> str:
    """Upload a file to the agent workspace bucket and return its GCS URI."""
    client = get_gcs_client()
    bucket_name = workspace_bucket_name(pid)
    bucket = client.bucket(bucket_name)
    blob_path = f"{agent_id}/{filename}"
    blob = bucket.blob(blob_path)
    blob.upload_from_string(data)
    return f"gs://{bucket_name}/{blob_path}"


async def list_workspace_files(pid: str, agent_id: str) -> list[dict]:
    client = get_gcs_client()
    bucket_name = workspace_bucket_name(pid)
    prefix = f"{agent_id}/"
    blobs = client.list_blobs(bucket_name, prefix=prefix)
    return [
        {
            "name": blob.name.removeprefix(prefix),
            "uri": f"gs://{bucket_name}/{blob.name}",
            "size": blob.size,
            "updated": blob.updated.isoformat() if blob.updated else None,
        }
        for blob in blobs
    ]


async def delete_workspace_file(pid: str, agent_id: str, filename: str):
    client = get_gcs_client()
    bucket_name = workspace_bucket_name(pid)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"{agent_id}/{filename}")
    blob.delete()


def resource_bucket_name() -> str:
    """Shared bucket for all goal resources (not per-goal)."""
    return get_settings().gcs_resource_bucket


async def upload_resource_file(resource_id: str, filename: str, data: bytes, content_type: str = "") -> str:
    """Upload a resource file to the shared resource bucket. Returns GCS URI."""
    client = get_gcs_client()
    bucket_name = resource_bucket_name()
    bucket = client.bucket(bucket_name)
    blob_path = f"{resource_id}/{filename}"
    blob = bucket.blob(blob_path)
    blob.upload_from_string(data, content_type=content_type or "application/octet-stream")
    return f"gs://{bucket_name}/{blob_path}"


def download_resource_uri(uri: str) -> bytes:
    """Read a `gs://bucket/path` or `artifact://<sha256>` object. SYNCHRONOUS.

    For the workspace drivers, which are synchronous by design -- the
    worker calls `acquire()` off the event loop -- so an async fetcher
    would have to be bridged at exactly the wrong layer.

    Takes a URI rather than (resource_id, filename) because that is what
    a mount carries: `goal_resources.uri` is the full gs:// path and the
    mount is derived from it. Splitting it back into two halves here
    would invent a parsing rule that the writer never agreed to.
    """
    # `artifact://<sha256>` IS A RESOURCE URI NOW. D2 made every emitted
    # report, dataset and method a grantable resource with that scheme,
    # and a story granted one mounts it `copyin` at docs/<name>, which
    # lands here for the bytes. Without this branch the whole
    # errand-report-then-story chain refused at its last link, with a
    # message about a URI scheme rather than about a missing report.
    #
    # No download: the content store is a shared tree this process can
    # open, so resolving is finding the file.
    from kernel.artifacts.uri import is_artifact_uri, read_artifact_uri

    if is_artifact_uri(uri):
        return read_artifact_uri(uri)
    if not (uri or "").startswith("gs://"):
        raise ValueError(f"not a gs:// or artifact:// uri: {uri!r}")
    bucket_name, _, blob_path = uri[len("gs://"):].partition("/")
    if not blob_path:
        raise ValueError(f"uri names a bucket but no object: {uri!r}")
    client = get_gcs_client()
    return client.bucket(bucket_name).blob(blob_path).download_as_bytes()


async def download_resource_file(resource_id: str, filename: str) -> bytes:
    """Download a resource file from the shared resource bucket. Returns raw bytes.

    Used for re-entry rehydration: when a Claude workspace is rebuilt after a
    long gate pause, the orchestrator downloads the merge report (or prior
    integration artifacts) from GCS back into the workspace so SHCA can read it.
    See documentation/story-integration/DECISION_TRACEABILITY.md.
    """
    client = get_gcs_client()
    bucket_name = resource_bucket_name()
    bucket = client.bucket(bucket_name)
    blob_path = f"{resource_id}/{filename}"
    blob = bucket.blob(blob_path)
    return blob.download_as_bytes()


async def generate_resource_signed_url(gcs_uri: str, expiry_minutes: int = 120) -> str | None:
    """Generate a short-lived V4 signed URL for a GCS resource blob.

    Works on Cloud Run via the IAM signBlob endpoint (no key file needed) and
    locally when GOOGLE_APPLICATION_CREDENTIALS points to a service-account key.
    Returns None if the URI is absent, malformed, or signing fails — callers
    should degrade gracefully (e.g. show a plain download link instead).
    """
    if not gcs_uri or not gcs_uri.startswith("gs://"):
        return None
    without_scheme = gcs_uri[5:]  # strip "gs://"
    bucket_name, _, blob_path = without_scheme.partition("/")
    if not blob_path:
        return None

    def _sign() -> str:
        client = get_gcs_client()
        blob = client.bucket(bucket_name).blob(blob_path)
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(minutes=expiry_minutes),
            method="GET",
        )

    try:
        url = await asyncio.get_event_loop().run_in_executor(None, _sign)
        _log.info("signed GCS URL for %s (expiry %dm)", gcs_uri, expiry_minutes)
        return url
    except Exception:
        _log.warning("failed to sign GCS URL for %s", gcs_uri, exc_info=True)
        return None


async def read_workspace_folder(pid: str, agent_id: str, folder: str, max_files: int = 20) -> list[dict]:
    """List and read all UTF-8 text files under `folder/` in an agent's workspace.

    Returns list of {name, content, size} for files that decode cleanly.
    Binary files and decode failures are skipped silently.
    """
    client = get_gcs_client()
    bucket_name = workspace_bucket_name(pid)
    folder = folder.strip("/")
    prefix = f"{agent_id}/{folder}/"

    def _fetch_all() -> list[dict]:
        results: list[dict] = []
        try:
            blobs = list(client.list_blobs(bucket_name, prefix=prefix))
        except Exception:
            return results
        for blob in blobs:
            if len(results) >= max_files:
                break
            name = blob.name.removeprefix(prefix)
            if not name:
                continue
            try:
                data = blob.download_as_bytes()
                text = data.decode("utf-8")
                results.append({"name": name, "content": text, "size": blob.size or 0})
            except (UnicodeDecodeError, Exception):
                pass
        return results

    return await asyncio.get_event_loop().run_in_executor(None, _fetch_all)


async def download_gcs_uri_bytes(gcs_uri: str) -> bytes:
    """Download raw bytes from an arbitrary gs:// URI.

    Used for pre-fetching small text files (markdown, JSON, etc.) into the
    resource reader template at page-load time rather than a separate API call.
    """
    if not gcs_uri or not gcs_uri.startswith("gs://"):
        raise ValueError(f"not a GCS URI: {gcs_uri!r}")
    without_scheme = gcs_uri[5:]
    bucket_name, _, blob_path = without_scheme.partition("/")
    if not blob_path:
        raise ValueError(f"missing blob path in URI: {gcs_uri!r}")

    def _download() -> bytes:
        client = get_gcs_client()
        return client.bucket(bucket_name).blob(blob_path).download_as_bytes()

    return await asyncio.get_event_loop().run_in_executor(None, _download)
