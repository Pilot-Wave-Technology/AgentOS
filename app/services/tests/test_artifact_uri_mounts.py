"""An artifact granted to a story reaches that story's workspace.

THE LAST LINK OF THE CHAIN THE ERRAND SHAPE EXISTS FOR. "Investigate X,
then fix what you find" is an errand whose report is the input to a
story. D2 built every link but this one:

  emit -> manifest -> `art_<hash>` resource (uri `artifact://<sha256>`)
  -> grant_resource -> mount `copyin` at docs/<name> -> the host's
  object fetcher -> **ValueError: not a gs:// uri**

`app.storage.download_resource_uri` accepted `gs://` and nothing else,
so a story granted an errand's report failed to provision its workspace,
and the message named a URI scheme rather than a missing report.

Found on KER25 with three reports granted to story 4a975a86, before the
run dispatched. It would have failed identically whichever way the
resource was granted -- GO hand-registering duplicates hid the bug, it
did not cause it.
"""
from __future__ import annotations

import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_the_scheme_is_recognised():
    from kernel.artifacts.uri import artifact_id_of, is_artifact_uri

    assert is_artifact_uri("artifact://abc123")
    assert not is_artifact_uri("gs://bucket/key")
    assert not is_artifact_uri("")
    assert not is_artifact_uri(None)
    assert artifact_id_of("artifact://abc123") == "abc123"
    # A trailing path must not become part of the hash.
    assert artifact_id_of("artifact://abc123/report.md") == "abc123"
    assert artifact_id_of("artifact://abc123?v=2") == "abc123"


def test_the_fetcher_takes_both_schemes():
    """The mount path calls ONE fetcher for every document, so the
    branch has to live where that fetcher already is."""
    src = io.open(ROOT / "app/storage.py", encoding="utf-8").read()
    body = src[src.index("def download_resource_uri"):]
    body = body[:body.index("\nasync def ", 10)]
    assert "is_artifact_uri" in body, (
        "download_resource_uri does not recognise artifact:// -- a "
        "granted artifact cannot reach a workspace")
    # And it must still refuse a scheme it cannot serve, rather than
    # falling through to a GCS call that fails less clearly.
    assert "raise ValueError" in body


def test_a_missing_object_names_the_store():
    """Two very different causes wear one symptom: a store this process
    cannot open (the root was wrong for months, and every artifact read
    refused) versus an id that was never written. The message has to
    separate them."""
    from kernel.artifacts.uri import read_artifact_uri

    with pytest.raises(FileNotFoundError) as e:
        read_artifact_uri("artifact://" + "0" * 64)
    assert "content store at" in str(e.value)


def test_an_empty_id_is_refused_before_the_store_is_asked():
    from kernel.artifacts.uri import read_artifact_uri

    with pytest.raises(ValueError):
        read_artifact_uri("artifact://")


def test_a_document_with_an_artifact_uri_mounts_as_copyin():
    """`mount_for` already routes it; this pins that the two halves
    agree, so a change to either is caught here rather than in a run."""
    from kernel.workspace.mounts import mount_for

    m = mount_for("document", "artifact://" + "a" * 64, ref=None,
                  writable=False, resource_id="art_aaaaaaaaaaaa")
    assert m.binding == "copyin"
    assert m.at.startswith("docs/")


def test_the_helper_lives_in_kernel_not_app():
    """E0's container ships `kernel/` and NOT `app/`. `kernel/go/read.py`
    needs this helper, so putting it under `app/services/` raised
    ImportError in the one place the kernel runs alone — caught by
    asking what the container actually contains, not what the repo does.

    app -> kernel is the allowed direction; kernel -> app is not."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    assert (root / "kernel/artifacts/uri.py").exists()
    assert not (root / "app/services/artifact_uri.py").exists()
    for rel in ("kernel/go/read.py", "kernel/workspace/mounts.py",
                "kernel/harness/emit.py"):
        src = io.open(root / rel, encoding="utf-8").read()
        assert "from app." not in src and "import app." not in src, (
            f"{rel} imports from app/, which E0's container does not have")


def test_a_granted_artifact_mounts_under_its_own_name():
    """docs/<64 hex characters> x3 is a workspace the executor has to
    open blind — the shape that produces "I read the coverage report"
    when the fuzzing one was opened."""
    from kernel.workspace.mounts import mount_for

    m = mount_for("document", "artifact://" + "a" * 64,
                  name="coverage_and_gaps_audit_report")
    assert m.at == "docs/coverage_and_gaps_audit_report"
    # The SQL path derives the same thing; both are asserted because
    # they are two implementations of one rule.
    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "kernel/workspace/mounts.py", encoding="utf-8").read()
    assert "original_name" in src, (
        "the SQL derivation does not use the artifact's real name")
