"""E7 — the artifact store: outputs too big to carry in context.

The seam is two operations, `put` and `get`, and an id that is the sha256
of the content. Everything above the seam — `step_journal.result_ref`, the
Verifier's `artifact` criterion, `journal_read` — sees the same id whichever
binding is underneath.

WHY CONTENT-ADDRESSED RATHER THAN A PATH. A path is host-local, and four
different readers need these bytes: the executor on resume (possibly on
another host), the Verifier evaluating an `artifact` criterion, the control
panel showing a human, and `journal_read` following a result_ref from a leg
that ran elsewhere. `result_ref` lives in Postgres, which is shared, so a
path would be a global index pointing at local files. A hash is the same
everywhere. It also dedups for free: the same failing test suite run twice
is one object.

BINDINGS. FilesystemStore is E0's, because E0 stays cloud-free. Production
swaps in object storage; the ids do not change, so nothing above moves.
The worker is always the writer — the sandbox holds zero credentials and
owns no state, and that stays true when the binding is a bucket.

WHAT THIS DOES NOT DO. No garbage collection. Artifacts are referenced from
the journal, which outlives the story, so deleting them when a story
completes would leave the archive full of dangling references — a failure
nobody notices until they need the evidence. A full disk is visible; a
broken archive is not. Retention is a policy for when disk is actually a
problem.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Outputs below this go inline in the tool result and get no artifact.
# A 200-byte `ls` does not need an object.
INLINE_THRESHOLD = 32 * 1024

# Refused rather than stored. Hashing happens in memory on the worker, so
# a genuinely enormous payload is a problem to decline, not to absorb.
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


class ArtifactTooLarge(Exception):
    """The payload exceeds what the store will accept."""


class ArtifactNotFound(Exception):
    """No artifact with that id."""


class ArtifactCorrupt(Exception):
    """The bytes stored under an id do not hash to it."""


def compute_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verified(artifact_id: str, data: bytes) -> bytes:
    """The bytes, if they are the ones the id names. Otherwise refuse.

    CONTENT-ADDRESSING IS ONLY A GUARANTEE IF SOMEBODY CHECKS. Every id
    here is the sha256 of the content, so re-hashing on read makes the
    store tamper-evident for the cost of one pass over bytes already in
    memory — and there is now a pod holding a write credential that also
    runs repository-authored setup scripts, which is precisely the
    situation this closes.

    Without it, an object could be replaced under its own name and
    everything downstream would believe it: an `artifact` criterion
    checking a poisoned object, the control panel showing a person
    evidence that was edited, a criterion running against a tree that is
    not the committed one.

    `read_range` CANNOT DO THIS and does not pretend to — a partial read
    has nothing to hash against. Callers that need the guarantee use
    `get`, which is every caller for which the bytes are evidence.
    """
    actual = compute_id(data)
    if actual != artifact_id:
        raise ArtifactCorrupt(
            f"the object stored as {artifact_id[:12]} hashes to "
            f"{actual[:12]}: these are not the bytes that were written")
    return data


class FilesystemStore:
    """Content-addressed files under one root. E0's binding."""

    def __init__(self, root: str):
        self.root = root

    def _path(self, artifact_id: str) -> str:
        # Sharded two levels: a flat directory of a million files is slow
        # to list and unpleasant to inspect by hand.
        return os.path.join(self.root, artifact_id[:2], artifact_id[2:4],
                            artifact_id)

    def put(self, data: bytes) -> str:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactTooLarge(
                f"{len(data)} bytes exceeds the {MAX_ARTIFACT_BYTES} limit"
            )
        artifact_id = compute_id(data)
        path = self._path(artifact_id)

        if os.path.exists(path):
            # Same content, same id — already stored. This is the dedup.
            return artifact_id

        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write to a temp file in the same directory and rename. A reader
        # must never see a half-written artifact under a name that promises
        # a specific hash.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info("artifact stored: %s (%d bytes)", artifact_id[:12], len(data))
        return artifact_id

    def exists(self, artifact_id: str) -> bool:
        return os.path.isfile(self._path(artifact_id))

    def get(self, artifact_id: str) -> bytes:
        try:
            with open(self._path(artifact_id), "rb") as fh:
                return verified(artifact_id, fh.read())
        except FileNotFoundError:
            raise ArtifactNotFound(artifact_id) from None

    def read_range(self, artifact_id: str, offset: int = 0,
                   length: int | None = None) -> bytes:
        """Read part of an artifact without loading all of it.

        This is what makes a 50 MB log usable: head, tail and slice are
        ranges, not a full read followed by slicing in memory.
        """
        path = self._path(artifact_id)
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, offset))
                return fh.read() if length is None else fh.read(length)
        except FileNotFoundError:
            raise ArtifactNotFound(artifact_id) from None

    def size(self, artifact_id: str) -> int:
        try:
            return os.path.getsize(self._path(artifact_id))
        except FileNotFoundError:
            raise ArtifactNotFound(artifact_id) from None


# Root for the process. B1: no default inside environ.get — the fallback is
# spelled out here so it is visible in the code rather than hidden in a call.
_ROOT_ENV = os.environ.get("ARTIFACT_ROOT")
DEFAULT_ROOT = _ROOT_ENV or "/artifacts"

# WHICH BINDING. B1: no default inside environ.get — the fallback is
# spelled out here so the value the process runs with is visible.
_BINDING_ENV = os.environ.get("ARTIFACT_STORE")
BINDING = (_BINDING_ENV or "filesystem").strip().lower()

BINDINGS = ("filesystem", "gcs")

_store = None


class UnknownBinding(Exception):
    """ARTIFACT_STORE names a binding that does not exist.

    BY NAME, and refused rather than defaulted, for the reason the driver
    registry gives: a typo that silently falls back to the filesystem
    binding on a machine with no shared disk writes every artifact to a
    container that is about to be destroyed, and the first symptom is a
    journal full of result_refs pointing at nothing.
    """


def _build():
    if BINDING == "filesystem":
        return FilesystemStore(DEFAULT_ROOT)
    if BINDING == "gcs":
        # Imported here, not at module scope: E0 has no
        # google-cloud-storage and E5's module-load check imports every
        # module under kernel/.
        from kernel.artifacts.gcs import GcsStore

        # KeyError, deliberately. A bucket name is not a thing to guess:
        # the wrong one is a permission error at the first write, in a
        # leg, rather than a refusal at startup.
        bucket = os.environ["ARTIFACT_BUCKET"]
        prefix = os.environ.get("ARTIFACT_PREFIX") or ""
        return GcsStore(bucket, prefix)
    raise UnknownBinding(
        f"ARTIFACT_STORE={BINDING!r} is not one of {', '.join(BINDINGS)}")


def describe() -> str:
    """Which store this process is using, in one phrase.

    FOR REFUSALS, and it exists because one was unactionable. The
    control panel had no ARTIFACT_STORE, so `BINDING` fell back to
    "filesystem" and `ROOT` to "/artifacts" -- an empty directory on a
    Cloud Run container -- while the conductor wrote every artifact to
    a bucket. `read_artifact` then refused with

        no artifact '44f6ae02...' in the store

    which reads as "the bytes were never written" and sent everyone
    looking at the writer. The bytes were in the bucket the whole time.
    A refusal that names the store it searched turns that into a glance.
    """
    if BINDING == "gcs":
        bucket = os.environ.get("ARTIFACT_BUCKET") or "<ARTIFACT_BUCKET unset>"
        prefix = os.environ.get("ARTIFACT_PREFIX") or ""
        return f"the gcs store gs://{bucket}/{prefix}".rstrip("/")
    return f"the filesystem store at {DEFAULT_ROOT}"


def get_store():
    """The process's store, built from ARTIFACT_STORE.

    THE FIVE METHODS ARE SYNCHRONOUS AND THAT IS DELIBERATE — the
    filesystem binding has nothing to await. On a bucket they block on
    the network, and the worker is an event loop, so an async caller must
    go through kernel/artifacts/aio.py rather than calling these
    directly. See that module for why a blocking `get` in a leg stalls
    every other leg in the process.
    """
    global _store
    if _store is None:
        _store = _build()
    return _store


def set_store(store) -> None:
    """Point at another binding or another root. Used by tests."""
    global _store
    _store = store
