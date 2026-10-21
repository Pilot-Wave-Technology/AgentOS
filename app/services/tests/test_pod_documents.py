"""A document reaches a workspace composed inside a pod -- or is refused.

`CopyinDriver` fetches a `gs://` document through a fetcher the HOST
injects, and `app/main.py` is the only thing that injects one. A
Kubernetes execution pod composes its workspace in an initContainer
running pure kernel code with no `app/` in the image, so every document
mount refused there -- and a story planned without the documents it was
granted invents the rules those documents existed to state. That has
happened: see the `document` vs `doc` comment in
kernel/workspace/mounts.py.

I3 adds a kernel-level fetcher on the pod's own identity, BELOW the
host's in precedence, and refuses a local-directory source at admission
instead of letting it become a pod that never becomes Ready.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from kernel.worker.runner import WorkspaceUnsupported, prepare_workspace
from kernel.workspace import bindings, object_fetch
from kernel.workspace.driver import WorkspaceError, WorkspaceSpec
from kernel.workspace.mounts import Mount


@pytest.fixture(autouse=True)
def _no_fetchers(monkeypatch):
    """Both slots empty, and empty again afterwards.

    They are module globals, and app/main.py fills one at startup: a
    test that left either set would decide the outcome of the next file
    to run a copyin mount.
    """
    monkeypatch.setattr(bindings, "_object_fetcher", None)
    monkeypatch.setattr(bindings, "_kernel_object_fetcher", None)
    # The HTTP fallback reads these; unset so the third link refuses
    # rather than dialling something.
    monkeypatch.delenv("RESOURCE_OBJECT_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_AUTH_TOKEN", raising=False)


class _Blob:
    def __init__(self, body: bytes):
        self.body = body

    def download_as_bytes(self) -> bytes:
        return self.body


class _Bucket:
    def __init__(self, name: str, objects: dict, seen: list):
        self.name = name
        self.objects = objects
        self.seen = seen

    def blob(self, key: str) -> _Blob:
        self.seen.append("gs://" + self.name + "/" + key)
        return _Blob(self.objects[key])


class _Client:
    """Stands in for google.cloud.storage.Client. No network, ever."""

    def __init__(self, objects: dict):
        self.objects = objects
        self.seen: list = []

    def bucket(self, name: str) -> _Bucket:
        return _Bucket(name, self.objects, self.seen)


def _fake_gcs(monkeypatch, objects: dict) -> _Client:
    client = _Client(objects)
    # Patched on the module the fetcher imports from, because the import
    # happens INSIDE the function (E0 has no SDK) -- so the lookup is at
    # call time and this takes effect.
    monkeypatch.setattr("kernel.artifacts.gcs.storage_client",
                        lambda: client)
    return client


def _doc_spec(root) -> WorkspaceSpec:
    return WorkspaceSpec(execution_id=7, root=str(root),
                         source="gs://resources-next/goal-1/rules.md",
                         story_id="st-1", resource_id="res-1")


def test_a_gs_document_lands_with_no_host_fetcher(tmp_path, monkeypatch):
    """THE WHOLE POINT. In the pod nothing injects a fetcher, and until
    now that meant the mount refused and the story ran blind."""
    _fake_gcs(monkeypatch, {"goal-1/rules.md": b"# the rules\n"})
    object_fetch.register()

    dest = tmp_path / "docs" / "rules-md"
    bindings.CopyinDriver().acquire(_doc_spec(dest))

    assert dest.read_bytes() == b"# the rules\n"


def test_the_hosts_fetcher_still_wins(tmp_path, monkeypatch):
    """The control panel's fetcher carries the story's grant; the
    kernel's carries a bucket-wide identity. Registering the second must
    not quietly move every cloud document read off the first."""
    client = _fake_gcs(monkeypatch, {"goal-1/rules.md": b"from the bucket"})
    object_fetch.register()
    bindings.set_object_fetcher(lambda uri: b"from the control panel")

    dest = tmp_path / "docs" / "rules-md"
    bindings.CopyinDriver().acquire(_doc_spec(dest))

    assert dest.read_bytes() == b"from the control panel"
    assert client.seen == [], "the kernel fetcher read the bucket anyway"


def test_an_artifact_uri_is_not_claimed_by_the_kernel_fetcher():
    """`artifact://` resolves through the kernel's content store, which
    needs no bucket. A fetcher that raised on a scheme it does not own
    would break a working case in the act of adding a new one."""
    assert object_fetch.fetch_gs("artifact://" + "a" * 64) is None


def test_an_artifact_document_is_read_from_the_store_not_over_http(
        tmp_path, monkeypatch):
    """`artifact://` resolves on the pod's own identity.

    SUPERSEDES an earlier version of this test, which asserted the
    opposite -- that an artifact mount falls through to the HTTP link.
    That was written to a brief of mine claiming "the pod already reads
    the artifact store natively", and it did not: nothing in the chain
    had an `artifact://` branch, so an artifact-sourced document needed
    RESOURCE_OBJECT_URL and a control-panel token INSIDE an execution
    pod. The old test pinned the defect faithfully; the brief was wrong,
    not the test.

    The capability was already there -- `read_artifact_uri`, and the pod
    runs with ARTIFACT_STORE=gcs for the verifier's trees -- so what
    landed is the branch, not a new road.
    """
    client = _fake_gcs(monkeypatch, {})
    object_fetch.register()

    seen = {}

    def _read(uri):
        seen["uri"] = uri
        return b"the artifact bytes"

    monkeypatch.setattr("kernel.artifacts.uri.read_artifact_uri", _read)

    uri = "artifact://" + "b" * 64
    spec = WorkspaceSpec(execution_id=7, root=str(tmp_path / "docs" / "r"),
                         source=uri, story_id="st-1", resource_id="res-1")
    bindings.CopyinDriver().acquire(spec)

    assert seen["uri"] == uri
    assert (tmp_path / "docs" / "r").read_bytes() == b"the artifact bytes"
    # Still not the bucket: two schemes, two roads, one fetcher.
    assert client.seen == []


def test_a_uri_naming_no_object_is_refused_before_the_sdk(monkeypatch):
    """`gs://bucket` with no key answers 404 at the SDK, which reads as
    a missing document rather than a malformed resource row."""
    _fake_gcs(monkeypatch, {})
    with pytest.raises(WorkspaceError) as e:
        object_fetch.fetch_gs("gs://resources-next")
    assert "does not name an object" in str(e.value)


def test_a_read_the_identity_cannot_do_names_the_identity(monkeypatch):
    """A 403 because `agentos-execution` was never granted read on the
    bucket is configuration outside this repository -- the one cause a
    reader will not think of, so the message says it.

    THE RAISE WAS WRONG, THE MESSAGE WAS RIGHT. This used to assert that
    `fetch_gs` RAISES. That broke execution 241: in an execution pod
    there is no ambient identity at all -- the NetworkPolicy blocks the
    metadata server on purpose, because the sandbox shares the network
    namespace -- so `storage.Client()` raises EnvironmentError("Project
    was not passed"), which ended the chain before the control panel,
    the one link that CAN serve the document, was ever reached.

    So the fetcher declines, and the reason travels with the decline
    instead of dying in a log line: the string a person sees is the
    WorkspaceError, surfaced as `workspace-compose exited 1: workspace
    not composed: <this>`.
    """

    class _Denied(_Client):
        def bucket(self, name):
            raise RuntimeError("403 Forbidden")

    monkeypatch.setattr("kernel.artifacts.gcs.storage_client",
                        lambda: _Denied({}))
    uri = "gs://resources-next/goal-1/rules.md"
    assert object_fetch.fetch_gs(uri) is None, (
        "a document this process cannot read must fall through to the "
        "control panel, not end the chain")

    msg = object_fetch.why_declined(uri)
    assert "Workload Identity" in msg and "403" in msg, (
        f"the decline does not name the identity: {msg!r}")


def test_the_reason_reaches_the_error_a_person_reads(monkeypatch):
    """The diagnostic is only worth keeping if it arrives.

    `_fetch_object`'s refusal is the text that surfaces on a failed
    compose. A reason recorded and never attached to it is a reason
    nobody reads.
    """
    from kernel.workspace import bindings

    uri = "gs://resources-next/goal-1/rules.md"

    class _Denied(_Client):
        def bucket(self, name):
            raise RuntimeError("403 Forbidden")

    monkeypatch.setattr("kernel.artifacts.gcs.storage_client",
                        lambda: _Denied({}))
    monkeypatch.setattr(bindings, "_object_fetcher", None, raising=False)
    monkeypatch.setattr(bindings, "_kernel_object_fetcher",
                        object_fetch.fetch, raising=False)
    monkeypatch.setattr(bindings, "_fetch_over_http", lambda spec: b"")

    spec = type("S", (), {"source": uri, "story_id": "s1",
                          "resource_id": "r1"})()
    with pytest.raises(WorkspaceError) as caught:
        bindings._fetch_object(spec)
    text = str(caught.value)
    assert "planned on" in text, "the loud refusal was lost"
    assert "403" in text, (
        f"the chain refused without saying what the last link reported, "
        f"which is the afternoon-costing shape: {text!r}")


def test_the_module_imports_without_a_cloud_sdk():
    """It is imported by E5's module-load check like everything under
    kernel/, on a machine with no google-cloud-storage -- so the client
    is resolved inside the function, not at module scope."""
    source = pathlib.Path(object_fetch.__file__).read_text(encoding="utf-8")
    top = {n for node in ast.parse(source).body
           if isinstance(node, (ast.Import, ast.ImportFrom))
           for n in ([node.module] if isinstance(node, ast.ImportFrom)
                     else [a.name for a in node.names])}
    assert not any((m or "").startswith(("google", "kernel.artifacts"))
                   for m in top), f"a cloud import at module scope: {top}"


# ── the local-directory source, refused at admission ─────────────────


class _Conn:
    async def fetchrow(self, sql, *a):
        return {"workspace_source": None, "workspace_path": None}

    async def fetchval(self, sql, *a):
        return None

    async def execute(self, sql, *a):
        return "UPDATE 1"


class _PodDriver:
    WORKSPACE_MODEL = "pod"


def _mounts(*items):
    async def _mounts_for(conn, execution_id):
        return items
    return _mounts_for


@pytest.mark.asyncio
async def test_a_local_directory_source_refuses_by_name(monkeypatch):
    """The directory is on the conductor and the workspace is composed
    in a pod. Refused where the mount list is known -- before a pod
    exists -- because a pod stuck short of Ready reads as cluster
    trouble and sends a person to the wrong system."""
    doc = Mount(at="docs/handbook-pdf", binding="copyin",
                source="/srv/uploads/handbook.pdf", kind="document",
                resource_id="res-9")
    monkeypatch.setattr("kernel.worker.runner.mounts_for", _mounts(doc))

    def _boom(*a, **k):
        raise AssertionError("the filesystem was touched before refusing")

    monkeypatch.setattr("os.makedirs", _boom)

    with pytest.raises(WorkspaceUnsupported) as e:
        await prepare_workspace(_Conn(), 7, "story", "business", "/workspace",
                                driver=_PodDriver())
    msg = str(e.value)
    assert "docs/handbook-pdf" in msg and "/srv/uploads/handbook.pdf" in msg
    assert "res-9" in msg
    # It must say WHY, and it must not be the generic pod refusal: that
    # one stops being true when the initContainer lands, and it does not
    # tell anyone to re-grant the resource.
    assert "LOCAL DIRECTORY" in msg and "gs://" in msg
    assert "ReadWriteOncePod" not in msg


@pytest.mark.asyncio
async def test_a_gs_document_is_not_refused_at_admission(monkeypatch):
    """Only the local path is unreachable. A bucket document is exactly
    what the kernel fetcher handles, so it must not be caught by the
    same net.

    UPDATED BY I2. This read "it falls to the general pod refusal
    instead" and asserted `WorkspaceUnsupported` with the local-path
    wording absent -- true while nothing populated the claim. I2 removed
    that general refusal: the pod composes its own workspace now, so a
    bucket document is not refused at all. Asserting no refusal is the
    stronger statement and the one the original was reaching for.
    """
    doc = Mount(at="docs/rules-md", binding="copyin",
                source="gs://resources-next/goal-1/rules.md",
                kind="document", resource_id="res-1")
    monkeypatch.setattr("kernel.worker.runner.mounts_for", _mounts(doc))

    path = await prepare_workspace(_Conn(), 7, "story", "business",
                                   "/workspace", driver=_PodDriver())
    assert path == "/workspace"


@pytest.mark.asyncio
async def test_the_local_path_check_is_only_for_pod_substrates(monkeypatch,
                                                               tmp_path):
    """Docker and E0 compose the workspace on the disk the directory is
    on. Copying from a local folder is the copyin binding working as
    designed there, and nothing about that changes."""
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "brief.md").write_text("hello", encoding="utf-8")
    doc = Mount(at="docs/corpus", binding="copyin", source=str(source),
                kind="document", resource_id="res-3")
    monkeypatch.setattr("kernel.worker.runner.mounts_for", _mounts(doc))

    async def _no_lease(conn, execution_id, path, provider):
        return None

    monkeypatch.setattr("kernel.credentials.issue.issue", _no_lease)

    root = await prepare_workspace(_Conn(), 7, "story", "business",
                                   str(tmp_path / "ws"), driver=None)
    assert (pathlib.Path(root) / "docs" / "corpus" / "brief.md").exists()
