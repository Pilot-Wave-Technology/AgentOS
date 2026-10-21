"""The artifact store, bound to a bucket. The production binding.

SAME IDS, SAME FIVE METHODS. An artifact id is the sha256 of the
content, so it means the same thing here as under FilesystemStore, and
nothing above the seam moves when the binding changes — `result_ref` in
Postgres keeps pointing at the same object whichever machine wrote it.
That is the whole reason the store is content-addressed rather than
path-addressed: four readers need those bytes and three of them are on
different hosts.

WHY THE SDK IS IMPORTED INSIDE THE METHODS, and not at the top.

E0's image deliberately carries four packages and google-cloud-storage
is not one of them — E0 stays cloud-free, which is the point of having
a filesystem binding at all. And E5's module-load check imports EVERY
module under kernel/ to prove the tree is importable. A module-level
`from google.cloud import storage` would therefore fail the gate on a
machine that is behaving exactly as designed, reported as "the kernel
does not import" rather than "this dependency is absent here".

The same reasoning `kernel/drivers/registry.py` already applies to
substrate drivers: import lazily, and when the import fails, say which
binding needed what.

WHAT IS NOT HERE. No garbage collection, matching FilesystemStore and
for the same reason — artifacts are referenced from the journal, which
outlives the story, so deleting on completion leaves dangling references
nobody notices until they need the evidence. On a bucket the answer is a
LIFECYCLE RULE, which is not a delete: Nearline at 90 days, Coldline at
a year. The objects stay readable at the same ids and the rate falls
from $0.020 to $0.010 to $0.004 per GB-month.

WHAT THE WORKER'S GRANT SHOULD BE. Object create and read on this one
bucket, and NOT list. The hash is then the capability: an escaped
sandbox can add garbage, and cannot enumerate another story's work.
"""

from __future__ import annotations

import logging

from kernel.artifacts.store import (MAX_ARTIFACT_BYTES, ArtifactNotFound,
                                    ArtifactTooLarge, compute_id, verified)

logger = logging.getLogger(__name__)


class GcsBindingUnavailable(Exception):
    """The bucket binding was asked for and its SDK is not installed."""


def _storage():
    """The SDK, or a refusal that names what is missing and why."""
    try:
        from google.cloud import storage                    # noqa: PLC0415
    except ImportError as exc:                              # pragma: no cover
        raise GcsBindingUnavailable(
            "the artifact store is bound to a bucket but "
            "google-cloud-storage is not installed. Install it, or set "
            "ARTIFACT_STORE=filesystem — E0 runs the filesystem binding "
            "on purpose and does not carry the SDK.") from exc
    return storage


def storage_client():
    """A GCS client on the ambient identity. THE kernel's way in.

    One construction site, because a second one drifts: this is
    `storage.Client()` with no arguments on purpose -- Workload Identity
    in a pod, the node's service account on a VM, ADC on a laptop -- and
    a caller that built its own client with explicit credentials would
    be carrying a secret the rest of the kernel deliberately does not.

    Public because `kernel/workspace/object_fetch.py` needs the same
    client to fetch a `gs://` document inside an execution pod, where no
    host is present to inject a fetcher. It raises
    `GcsBindingUnavailable` when the SDK is absent; that caller
    translates it, since "set ARTIFACT_STORE=filesystem" is the right
    advice for an artifact and not for a document.
    """
    return _storage().Client()


class GcsStore:
    """Content-addressed objects in one bucket."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        self.bucket_name = bucket
        # Trailing slash normalised here so `_name` can stay a join.
        self.prefix = prefix.strip("/")
        self._bucket = None

    def _client_bucket(self):
        """Resolved once, lazily. Constructing a client at import time
        would make an unconfigured environment fail on import rather than
        on use, which is the harder failure to read."""
        if self._bucket is None:
            self._bucket = storage_client().bucket(self.bucket_name)
        return self._bucket

    def _name(self, artifact_id: str) -> str:
        # The same two-level sharding the filesystem binding uses. GCS has
        # a flat namespace and does not need it for performance, but the
        # console renders `/` as folders, and an artifact tree a person
        # can open is worth the two characters.
        shard = f"{artifact_id[:2]}/{artifact_id[2:4]}/{artifact_id}"
        return f"{self.prefix}/{shard}" if self.prefix else shard

    def _blob(self, artifact_id: str):
        return self._client_bucket().blob(self._name(artifact_id))

    def put(self, data: bytes) -> str:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactTooLarge(
                f"{len(data)} bytes exceeds the {MAX_ARTIFACT_BYTES} limit")
        artifact_id = compute_id(data)
        blob = self._blob(artifact_id)

        # THE DEDUP, and it is also the write barrier. Same content means
        # same id means same object, so an existing one is already
        # correct and re-uploading it would only cost money. `if_generation_
        # match=0` on the upload makes the create conditional, so two
        # workers writing the same artifact at once cannot interleave a
        # half-written object under a name that promises a specific hash
        # — the filesystem binding gets that from rename-into-place.
        if blob.exists():
            return artifact_id
        try:
            blob.upload_from_string(data, if_generation_match=0)
        except Exception as exc:                            # noqa: BLE001
            # 412 means somebody else created it between the check and
            # the write. The content is identical by construction, so the
            # end state is the one we wanted.
            if "412" not in str(exc) and "precondition" not in str(exc).lower():
                raise
            logger.debug("artifact %s written concurrently", artifact_id[:12])
        logger.info("artifact stored: %s (%d bytes)",
                    artifact_id[:12], len(data))
        return artifact_id

    def exists(self, artifact_id: str) -> bool:
        return bool(self._blob(artifact_id).exists())

    def get(self, artifact_id: str) -> bytes:
        blob = self._blob(artifact_id)
        try:
            # Re-hashed like every other binding's read: the store is
            # content-addressed, and that is a guarantee only where
            # somebody checks. See store.verified.
            return verified(artifact_id, blob.download_as_bytes())
        except Exception as exc:                            # noqa: BLE001
            if _is_missing(exc):
                raise ArtifactNotFound(artifact_id) from None
            raise

    def read_range(self, artifact_id: str, offset: int = 0,
                   length: int | None = None) -> bytes:
        """Part of an artifact, as an HTTP Range request.

        This is what makes a 50 MB log usable — head, tail and slice are
        ranges rather than a full download sliced in memory, and on a
        bucket the difference is also the egress bill.
        """
        start = max(0, offset)
        end = None if length is None else start + length - 1
        try:
            return self._blob(artifact_id).download_as_bytes(
                start=start, end=end)
        except Exception as exc:                            # noqa: BLE001
            if _is_missing(exc):
                raise ArtifactNotFound(artifact_id) from None
            raise

    def size(self, artifact_id: str) -> int:
        blob = self._blob(artifact_id)
        blob.reload()
        if blob.size is None:
            raise ArtifactNotFound(artifact_id)
        return int(blob.size)


def _is_missing(exc: Exception) -> bool:
    """A 404 from the SDK, without importing its exception types.

    Checking `getattr(exc, "code", None) == 404` first because that is
    what google.api_core.exceptions.NotFound carries; the string check is
    the fallback for a transport that wrapped it.
    """
    if getattr(exc, "code", None) == 404:
        return True
    return "404" in str(exc) or "not found" in str(exc).lower()
