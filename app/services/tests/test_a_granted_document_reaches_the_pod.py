"""A document granted to a story arrives in its workspace.

WHAT FAILED, and it was the first time anyone tried. Executions 234 and
235 on 2026-09-20 were the first two ever to carry a document mount. Both
died in `workspace-compose`:

    artifact e95add9b33da is not in the content store at '/artifacts'

which reads as "the bytes were never written". The bytes were in the
bucket the whole time.

THE CHAIN, AND WHERE IT BROKE. `_fetch_object` tries three links: the
host's injected fetcher (the control panel, in-process), the substrate's
(a pod entrypoint, on the pod's own identity), then HTTP to the control
panel's `/internal/resource-object`. The second falls through to the
third ONLY by returning None.

`object_fetch.fetch` grew an `artifact://` branch on a premise its own
docstring states: "the pod already runs with ARTIFACT_STORE=gcs for the
verifier's trees". True of a VERIFICATION pod. False of an EXECUTION
one -- `spec_builder` sets that spec's binding to `filesystem` and pops
the bucket, deliberately, because the pod is denied the metadata server
and its sandbox shares the network namespace.

So the branch read a store that could not hold the object and RAISED.
Raising ends a chain. The one link that can serve an execution pod --
the broker, which reads the bucket with the CONTROL PANEL's identity and
authorises by grant rather than by URI -- was never reached.

The rule this restores is the one the same function states at its
bottom: "NOT AN ERROR. A scheme this cannot serve belongs to the next
link in the chain."
"""
from __future__ import annotations

import pytest

from kernel.workspace import object_fetch

MISSING = "artifact://" + "0" * 64


def test_an_artifact_this_store_cannot_serve_is_none_not_an_exception():
    """The fix, stated as the rule it restores.

    None means "not mine, try the next link". An exception means "the
    chain is over", and for an execution pod the chain was over before
    reaching the only link that works.
    """
    assert object_fetch.fetch(MISSING) is None


def test_a_scheme_it_does_not_own_is_also_none():
    """The rule was always right for schemes; only the artifact branch
    broke it."""
    assert object_fetch.fetch("http://example.invalid/doc.md") is None


def test_the_chain_falls_through_to_the_broker(monkeypatch):
    """The behaviour the None actually buys.

    Pins the fall-through itself rather than just the return value: a
    later change that made `_fetch_object` treat None as failure would
    leave this module correct and the pod still broken.
    """
    from kernel.workspace import bindings

    called: dict = {}

    def fake_http(spec):
        called["spec"] = spec
        return b"the document's bytes"

    monkeypatch.setattr(bindings, "_object_fetcher", None, raising=False)
    monkeypatch.setattr(bindings, "_kernel_object_fetcher",
                        lambda uri: None, raising=False)
    monkeypatch.setattr(bindings, "_fetch_over_http", fake_http)

    spec = type("S", (), {"source": MISSING, "story_id": "s1",
                          "resource_id": "art_0000"})()
    assert bindings._fetch_object(spec) == b"the document's bytes"
    assert called["spec"] is spec, "the broker was not reached"


def test_an_empty_fetch_still_refuses_loudly(monkeypatch):
    """Falling through must not become failing quietly.

    A workspace missing the document the story is written against
    produces work planned on a guess, which is worse than a refusal --
    the existing message says so and this keeps it.
    """
    from kernel.workspace import bindings

    monkeypatch.setattr(bindings, "_object_fetcher", None, raising=False)
    monkeypatch.setattr(bindings, "_kernel_object_fetcher",
                        lambda uri: None, raising=False)
    monkeypatch.setattr(bindings, "_fetch_over_http", lambda spec: b"")

    spec = type("S", (), {"source": MISSING, "story_id": "s1",
                          "resource_id": "art_0000"})()
    with pytest.raises(bindings.WorkspaceError) as caught:
        bindings._fetch_object(spec)
    assert "planned on" in str(caught.value)


def test_the_broker_is_addressed_by_grant_not_by_uri():
    """Why the broker can serve what the pod cannot.

    It takes (story_id, resource_id) and checks the story holds that
    grant, so the pod never names a bucket path and the endpoint never
    becomes an open proxy. That is also why an `artifact://` URI the pod
    cannot resolve is still servable: the control panel resolves the
    resource, with its own credential, on its own side.
    """
    import inspect

    from kernel.workspace import bindings

    src = inspect.getsource(bindings._fetch_over_http)
    assert "story_id=" in src and "resource_id=" in src, (
        "the broker call no longer identifies the grant; a URI-addressed "
        "fetch would make /internal/resource-object an open proxy for "
        "the resource bucket")

# ── the same rule, the other scheme ──────────────────────────────────

GS_URI = "gs://some-bucket/goals/g1/brief.md"


def test_a_bucket_document_this_process_cannot_read_is_none(monkeypatch):
    """Execution 241, as a test.

    `044fbc28` taught the `artifact://` branch to fall through and left
    `gs://` raising. An execution pod has no project and no ADC -- it is
    denied the metadata server on purpose -- so `storage.Client()`
    raises EnvironmentError("Project was not passed..."), which is NOT
    `GcsBindingUnavailable` and sailed straight out of the chain.

    The pod died in `workspace-compose`, and the surfaced error named
    `cloud-sql-proxy exited 143` -- the sidecar being SIGTERM'd as the
    pod tore down -- with the real cause last in the line.
    """
    import kernel.artifacts.gcs as gcs

    def no_project():
        raise EnvironmentError(
            "Project was not passed and could not be determined from "
            "the environment.")

    monkeypatch.setattr(gcs, "storage_client", no_project)
    assert object_fetch.fetch_gs(GS_URI) is None


def test_a_bucket_document_that_is_simply_absent_is_also_none(monkeypatch):
    """Owning the scheme is not being able to serve it.

    Whether the object is gone or this identity cannot read it, the
    answer to the CHAIN is the same: not mine. `_fetch_object` refuses
    loudly if no link serves it, which is where that refusal belongs.
    """
    import kernel.artifacts.gcs as gcs

    class _Boom:
        def bucket(self, *_a):
            raise RuntimeError("403 caller lacks storage.objects.get")

    monkeypatch.setattr(gcs, "storage_client", _Boom)
    assert object_fetch.fetch_gs(GS_URI) is None


def test_falling_through_is_not_falling_silent():
    """A miss that logs nothing costs an afternoon.

    `read_artifact_uri` reporting DEFAULT_ROOT on a GCS miss -- "not in
    the content store at '/artifacts'" while it searched a bucket -- is
    the cautionary case. Whichever link ends up refusing, the reason
    this one could not serve it has to be in the log.
    """
    import inspect

    src = inspect.getsource(object_fetch.fetch_gs)
    assert "logger.warning" in src, (
        "the unreadable-document path returns None without recording "
        "why, so a failed compose names no cause")
