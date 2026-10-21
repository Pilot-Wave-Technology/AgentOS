"""`artifact://<sha256>` — the scheme D2 introduced and nothing could fetch.

WHAT BROKE. D2 made every emitted report, dataset and method a grantable
goal resource, with `uri = artifact://<sha256>`. Granting one to a story
mounts it `copyin` at `docs/<name>`, and `copyin` asks the host's
injected object fetcher for the bytes. That fetcher is
`app.storage.download_resource_uri`, which opens with:

    if not (uri or "").startswith("gs://"):
        raise ValueError(f"not a gs:// uri: {uri!r}")

So the chain ran: artifact emitted, catalogued, granted, mounted — and
then refused, with a message about a URI scheme, a long way from "the
report could not be attached to the story that was told to act on it".

THE ERRAND SHAPE EXISTS FOR THIS CHAIN. "Investigate X, then fix what
you find" is an errand whose report is the input to a story. Every link
was built and the last one could not fetch.

WHY IT LIVES IN `kernel/`. It wraps the kernel's own content store,
and `kernel/go/read.py` needs it too. E0's container ships `kernel/`
and NOT `app/`, so a helper under `app/services/` imported from the
kernel raises ImportError in the one place the kernel runs alone --
caught here before it shipped, by asking what the container actually
contains rather than what the repo does. app -> kernel is the allowed
direction; kernel -> app is not.

WHY IT RESOLVES LOCALLY RATHER THAN OVER HTTP. The bytes are in the
content store, addressed by hash, and since the store became a shared
bind mount the control panel opens the same tree the executor writes.
There is nothing to download: the fetcher's job here is to find the file.

A gs:// resource still goes to the object store, untouched. This adds a
branch; it does not move anybody's cheese.
"""
from __future__ import annotations

SCHEME = "artifact://"


def is_artifact_uri(uri: str | None) -> bool:
    """Does this resource live in the kernel's content store?"""
    return (uri or "").startswith(SCHEME)


def artifact_id_of(uri: str) -> str:
    """The content hash a resource URI names.

    Tolerates a trailing path or query, so a later `artifact://<sha>/x`
    does not silently resolve to a hash with a slash in it.
    """
    rest = (uri or "")[len(SCHEME):]
    for sep in ("/", "?", "#"):
        rest = rest.split(sep, 1)[0]
    return rest.strip()


def read_artifact_uri(uri: str) -> bytes:
    """The bytes behind an `artifact://` resource.

    Raises `FileNotFoundError` when the store has no such object, which
    is what the caller can tell a person: the row exists and the content
    does not. `ArtifactNotFound` is the store's own type; it is
    translated here so callers need not import the kernel to catch it.
    """
    from kernel.artifacts.store import ArtifactNotFound, get_store

    artifact_id = artifact_id_of(uri)
    if not artifact_id:
        raise ValueError(f"artifact uri names no object: {uri!r}")
    try:
        return get_store().get(artifact_id)
    except ArtifactNotFound as exc:
        # NAMES THE STORE, because the same symptom has two very
        # different causes: a store this process cannot open (the root
        # was wrong for months, and every artifact read refused) versus
        # an id that was never written.
        from kernel.artifacts.store import DEFAULT_ROOT

        raise FileNotFoundError(
            f"artifact {artifact_id[:12]} is not in the content store at "
            f"{DEFAULT_ROOT!r}") from exc
