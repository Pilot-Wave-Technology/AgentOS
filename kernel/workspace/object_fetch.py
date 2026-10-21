"""A `gs://` document, fetched by the kernel itself. The pod's fetcher.

WHY THIS IS NOT A BREACH OF "kernel/ MUST NOT IMPORT app/". It reads a
bucket, and the kernel already does: `kernel/artifacts/store.py` binds
the artifact store to GCS under `ARTIFACT_STORE=gcs`, and
`kernel/artifacts/gcs.py` is the read and write path for it, ids and
all. The rule forbids depending on the control panel, not on cloud
storage. So this imports the SDK the one way that module already
established -- `storage_client()`, lazily, with the same refusal when it
is absent -- rather than opening a second route to GCS that would drift
from the first.

WHY IT EXISTS. `CopyinDriver.acquire` fetches a document mount through a
fetcher the HOST injects, and `app/main.py` is what injects it. In a
Kubernetes execution pod the workspace is composed by an initContainer
running pure kernel code with no `app/` in the image, so nothing
registers one and every `gs://` document refuses. That is not a quiet
loss: `kernel/workspace/mounts.py` records the observed case where a
goal's three documents failed to materialise and the story was planned
from the story text alone, "inventing the rules the documents existed to
state, and nothing anywhere said a word about it".

CREDENTIALS ARE AMBIENT, NEVER CARRIED. `storage.Client()` with no
arguments: Workload Identity in the pod (`agentos-execution`, granted
read on the environment's resource bucket) and ADC on a laptop. Nothing
is written to the workspace and nothing is passed through a mount, which
is the same pointer-not-value rule `kernel/workspace/git_bearer.py`
enforces for the broker bearer.

THE HOST'S FETCHER STILL WINS. See `set_kernel_object_fetcher` in
`kernel/workspace/bindings.py`: the control panel's fetcher carries
credentials and grant policy this one does not, so it takes precedence
wherever it exists and this is only the fallback.
"""

from __future__ import annotations

import logging

from kernel.workspace.driver import WorkspaceError

logger = logging.getLogger(__name__)

SCHEME = "gs://"


def split_uri(uri: str) -> tuple:
    """`gs://bucket/key` as (bucket, key), or a refusal naming the URI.

    A URI that names a bucket and no object is refused here rather than
    at the SDK, which answers it with a 404 on an empty blob name -- a
    "not found" for a request that was never well-formed sends a person
    looking for a missing document instead of at the resource row.
    """
    rest = uri[len(SCHEME):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise WorkspaceError(
            f"{uri!r} does not name an object: a document source must be "
            f"gs://<bucket>/<key>")
    return bucket, key


# WHY THE LAST LINK DECLINED, so the chain's refusal can say it.
#
# Returning None is right -- a process that cannot serve a URI must not
# end the chain -- but a reason that only reaches the log is a reason
# nobody reads. The message a person actually sees is the WorkspaceError
# text, which surfaces through `workspace-compose exited 1: workspace
# not composed: <this>`; that is how "Project was not passed" was found
# at all.
#
# ONE ENTRY, matched by URI. Composes are sequential in a process, and
# an unbounded map keyed by URI is a leak in a long-lived worker.
_LAST_DECLINE: tuple = ("", "")


def _decline(uri: str, reason: str):
    """Record why this process could not serve `uri`, and return None."""
    global _LAST_DECLINE
    _LAST_DECLINE = (uri, reason)
    return None


def why_declined(uri: str) -> str:
    """The reason this process last passed on `uri`, or ""."""
    return _LAST_DECLINE[1] if _LAST_DECLINE[0] == uri else ""


def fetch_gs(uri: str):
    """The document's bytes, or None when this URI is not ours.

    NONE RATHER THAN A REFUSAL for a scheme we do not own, because this
    is registered as ONE LINK IN A CHAIN (see `_fetch_object`). An
    `artifact://` document resolves through the path it already
    resolves through, and a fetcher that raised on it would break a
    working case in the act of adding a new one.
    """
    if not (uri or "").startswith(SCHEME):
        return None
    bucket, key = split_uri(uri)
    # IMPORTED HERE, NOT AT MODULE SCOPE. E0's image carries a minimal
    # dependency set and google-cloud-storage is not in it, while E5's
    # module-load check imports every module under kernel/ -- a
    # module-level SDK import would fail that gate on a machine
    # behaving exactly as designed, reported as "the kernel does not
    # import" rather than "this dependency is absent here". Same
    # reasoning kernel/artifacts/gcs.py and kernel/assistant/asgi.py
    # spell out for their own imports.
    from kernel.artifacts.gcs import GcsBindingUnavailable, storage_client

    # CANNOT SERVE IS `None`, NOT AN EXCEPTION -- for the client as much
    # as for the download. This is the SECOND time this module has been
    # fixed for the same rule: 044fbc28 taught the `artifact://` branch
    # below to fall through, and left this one raising.
    #
    # `storage_client()` is `storage.Client()` with no arguments, and in
    # an execution pod there is no project and no ADC -- the pod is
    # denied the metadata server on purpose. It does not raise
    # `GcsBindingUnavailable`; it raises EnvironmentError("Project was
    # not passed and could not be determined from the environment"),
    # which sailed past the one `except` here and out of the chain.
    #
    # Measured: execution 241 on 2026-09-20, reported as
    # "cloud-sql-proxy exited 143" -- the sidecar being SIGTERM'd as the
    # pod tore down -- with the real cause last in the line:
    # "workspace-compose exited 1: workspace not composed: Project was
    # not passed".
    #
    # OWNING THE SCHEME IS NOT BEING ABLE TO SERVE IT. The docstring
    # above draws the line at ownership, and that is the wrong line: an
    # execution pod owns `gs://` and can never read it. The control
    # panel can, and it is the next link.
    try:
        client = storage_client()
        blob = client.bucket(bucket).blob(key).download_as_bytes()
    except GcsBindingUnavailable as exc:
        logger.info("%s: google-cloud-storage is not installed here (%s); "
                    "leaving it to the next link in the chain", uri, exc)
        return _decline(
            uri, f"google-cloud-storage is not installed in this image "
                 f"({exc})")
    except Exception as exc:                                # noqa: BLE001
        # WARNING, NOT SILENCE. Falling through must not become failing
        # quietly: if no later link serves this either, `_fetch_object`
        # refuses the whole compose, and this line is what says why it
        # could not be read HERE -- the object is gone, or this identity
        # (Workload Identity in a pod, ADC locally) has no read on that
        # bucket, or there is no identity at all.
        logger.warning("%s could not be read with this process's own "
                       "identity (%s); leaving it to the next link in "
                       "the chain", uri, exc)
        # NAMES THE IDENTITY. The two likely causes look the same from
        # here and lead to different places: the object is genuinely
        # gone, or this identity has no read on that bucket -- a grant
        # outside this repository, and the cause a reader will not think
        # of. In an EXECUTION pod there is a third and it is normal:
        # there is no ambient identity at all, on purpose.
        return _decline(
            uri, f"this process could not read it with its own identity "
                 f"({exc}); either the object is gone, or the identity "
                 f"(Workload Identity in a pod, ADC locally) has no read "
                 f"on that bucket -- note an execution pod is denied the "
                 f"metadata server deliberately")
    logger.info("fetched %s (%d bytes) with the kernel's own identity",
                uri, len(blob))
    return blob


ARTIFACT_SCHEME = "artifact://"


def fetch(uri: str):
    """Everything a pod can read on its own identity. None for the rest.

    TWO SCHEMES, AND `artifact://` WAS ASSUMED RATHER THAN BUILT. P3/D4
    claimed "the pod already reads the artifact store natively", and it
    does not: nothing in the fetch chain had an `artifact://` branch, so
    an artifact-sourced document fell through to the HTTP link and
    needed RESOURCE_OBJECT_URL and a control-panel token inside an
    execution pod -- the opposite of what D4 decided. The capability was
    genuinely there (`kernel/artifacts/uri.read_artifact_uri`, and the
    pod already runs with ARTIFACT_STORE=gcs for the verifier's trees);
    only the branch was missing.

    An emitted artifact mounted into a later run is the ordinary case
    for this: `emit_artifact` registers what a run produced as a goal
    resource, and a story granted it gets `artifact://<hash>`.
    """
    if uri.startswith(SCHEME):
        return fetch_gs(uri)
    if uri.startswith(ARTIFACT_SCHEME):
        # Imported here for the reason fetch_gs's client is: the E0
        # container loads every module under kernel/ and must not need
        # a cloud SDK to do it.
        from kernel.artifacts.uri import read_artifact_uri  # noqa: PLC0415

        try:
            blob = read_artifact_uri(uri)
        except FileNotFoundError as exc:
            # A MISS IS `None`, WHICH IS THIS FUNCTION'S OWN RULE and the
            # one line above it used to break.
            #
            # The docstring above says "the pod already runs with
            # ARTIFACT_STORE=gcs for the verifier's trees". That is true
            # of a VERIFICATION pod and false of an EXECUTION one:
            # `kernel/conductor/spec_builder.py` sets the execution
            # spec's binding to `filesystem` and pops the bucket,
            # deliberately, because the pod is denied the metadata
            # server -- its sandbox shares the network namespace, and a
            # credential there is one a model can mint.
            #
            # So this branch read a store that cannot hold the object,
            # and RAISED. Raising ends the chain: `_fetch_object` falls
            # through to the control-panel broker only on `None`, so the
            # one link that CAN serve an execution pod was never
            # reached. The pod died in `workspace-compose` with
            #
            #     artifact e95add9b33da is not in the content store
            #     at '/artifacts'
            #
            # which reads as "the bytes were never written" and sends
            # the reader to the writer. The bytes were in the bucket.
            #
            # Measured: executions 234 and 235, the first two ever to
            # carry a document mount, on 2026-09-20.
            #
            # Returning None costs the verifier nothing -- its store IS
            # gcs, so the read succeeds and never arrives here -- and
            # costs E0 nothing, where the filesystem store genuinely
            # holds the object. It only changes the case where this
            # process cannot serve the URI, which is precisely what the
            # `return None` at the end of this function is for.
            logger.info("%s is not in this process's artifact store (%s); "
                        "leaving it to the next link in the chain", uri, exc)
            return None
        logger.info("read %s (%d bytes) from the artifact store",
                    uri, len(blob))
        return blob
    # NOT AN ERROR. A scheme this cannot serve belongs to the next link
    # in the chain, and refusing here would take `http://` off the road
    # it already travels.
    return None


def register() -> None:
    """Offer this fetcher to the copyin binding. For a pod entrypoint.

    Called by the initContainer that composes the workspace, which is
    the process that HAS the pod's identity. Registering it from the
    control panel would be wrong and is also unnecessary: the host's own
    fetcher wins there, and it is the one holding the grant policy.
    """
    from kernel.workspace import bindings

    bindings.set_kernel_object_fetcher(fetch)
