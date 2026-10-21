"""The initContainer's entry point: an id in, a tree on disk out.

This is the only code that runs between "the Verifier committed a tree"
and "a criterion ran against it", so the two things it must never do are
hand over the wrong bytes and hand them over quietly.
"""

from __future__ import annotations

import io
import os
import tarfile

import pytest

from kernel.artifacts import fetch
from kernel.artifacts.store import FilesystemStore, set_store


def _tar(files: dict, mode: int = 0o644) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    binding = FilesystemStore(str(tmp_path / "artifacts"))
    set_store(binding)
    yield binding
    set_store(None)


def test_the_tree_lands_where_the_criterion_expects_it(store, tmp_path):
    blob = _tar({"verify/pkg/a.py": b"print(1)\n"})
    artifact_id = store.put(blob)
    dest = tmp_path / "unpacked"

    assert fetch.unpack(artifact_id, str(dest)) == len(blob)
    assert (dest / "verify" / "pkg" / "a.py").read_bytes() == b"print(1)\n"


def test_the_executable_bit_survives_the_filter():
    """A prepared tree carries `.agentos/setup.sh` and whatever the
    repository's own runners are. A filter that stripped the bit would
    fail every check that shells out to one.

    ASSERTED ON THE FILTER, not on the extracted file. `os.access(...,
    X_OK)` returns True for a 0644 file on Windows, so the obvious
    version of this test passes whatever the filter does — which is the
    same vacuous-assertion trap Fb10 was built to catch.
    """
    executable = tarfile.TarInfo("verify/run.sh")
    executable.mode = 0o755
    plain = tarfile.TarInfo("verify/a.py")
    plain.mode = 0o644

    assert tarfile.data_filter(executable, "/dest").mode == 0o755
    assert tarfile.data_filter(plain, "/dest").mode == 0o644


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows honours only the read-only bit")
def test_the_extracted_file_really_is_executable(store, tmp_path):
    """The filter's answer, confirmed against a filesystem that has
    permissions. Skipped rather than weakened where it does not."""
    artifact_id = store.put(_tar({"verify/run.sh": b"#!/bin/sh\n"},
                                 mode=0o755))
    dest = tmp_path / "unpacked"
    fetch.unpack(artifact_id, str(dest))
    assert (dest / "verify" / "run.sh").stat().st_mode & 0o111


def test_bytes_that_do_not_hash_to_their_name_are_refused(store, tmp_path):
    """THE VERIFIER'S WHOLE CLAIM is that the criterion ran against the
    COMMITTED tree. Content-addressing makes the id name one object;
    re-hashing on arrival closes the one gap it leaves — an object
    replaced under its own name.

    The check lives in `store.get`, so it covers the control panel and
    the `artifact` criterion too. This asserts it reaches the fetcher.
    """
    real = store.put(_tar({"verify/a.py": b"good"}))
    # Overwrite the stored object with different bytes, keeping its name.
    with open(store._path(real), "wb") as handle:
        handle.write(_tar({"verify/a.py": b"evil"}))

    # LOOKED UP NOW, not bound at import. test_artifact_binding reloads
    # kernel.artifacts.store, which rebuilds its classes — a reference
    # captured at collection time is then a DIFFERENT object from the
    # one raised, and `pytest.raises` misses it. The failure is loud,
    # but only when the two files run in the same session.
    import kernel.artifacts.store as store_module

    with pytest.raises(store_module.ArtifactCorrupt):
        fetch.unpack(real, str(tmp_path / "unpacked"))


def test_a_path_that_escapes_the_destination_is_refused(store, tmp_path):
    """The tar is one we built, but it is built FROM A REPOSITORY, and a
    repository is exactly where a hostile path lands."""
    artifact_id = store.put(_tar({"../escaped.py": b"x"}))
    with pytest.raises(tarfile.OutsideDestinationError):
        fetch.unpack(artifact_id, str(tmp_path / "unpacked"))
    assert not (tmp_path / "escaped.py").exists()


def test_a_missing_artifact_fails_loudly(store, tmp_path, capsys):
    """An initContainer that exited 0 having fetched nothing leaves the
    criterion running against an empty directory and reporting the
    result as if it meant something."""
    assert fetch.main(["fetch", str(tmp_path / "d"), "0" * 64]) == 1
    assert "could not unpack" in capsys.readouterr().err


def test_pairs_are_required(store, tmp_path):
    """A destination with no id, or an id with no destination, is a
    caller bug and must not be guessed at."""
    assert fetch.main(["fetch", str(tmp_path)]) == 2
    assert fetch.main(["fetch"]) == 2


def test_several_artifacts_unpack_in_one_pass(store, tmp_path):
    """The tree and the replayed dependencies, which is the real case."""
    tree = store.put(_tar({"verify/a.py": b"1"}))
    deps = store.put(_tar({"deps/p.py": b"2"}))
    dest = str(tmp_path / "tmp")
    assert fetch.main(["fetch", dest, tree, dest, deps]) == 0
    assert (tmp_path / "tmp" / "verify" / "a.py").exists()
    assert (tmp_path / "tmp" / "deps" / "p.py").exists()


def test_the_module_imports_without_a_cloud_sdk():
    """It is imported by E5's module-load check like everything under
    kernel/, on a machine that has no google-cloud-storage — so the
    store is built inside `unpack`, not at module scope."""
    import ast
    import pathlib

    source = pathlib.Path(fetch.__file__).read_text(encoding="utf-8")
    top = {n for node in ast.parse(source).body
           if isinstance(node, (ast.Import, ast.ImportFrom))
           for n in ([node.module] if isinstance(node, ast.ImportFrom)
                     else [a.name for a in node.names])}
    assert not any((m or "").startswith(("google", "kernel.artifacts.gcs"))
                   for m in top), f"a cloud import at module scope: {top}"
    assert "kernel.artifacts.store" not in top, (
        "even the store is deferred: importing it pulls in the binding")
