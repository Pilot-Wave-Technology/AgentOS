"""The artifact store has two bindings and one set of ids.

WHY THIS MATTERS FOR THE SPLIT. The control panel reads the artifact
store IN ITS OWN PROCESS. With the control panel on Cloud Run and the
worker on GKE there is no shared filesystem between them, so the
object-storage binding is a prerequisite for the first cloud deploy
rather than a later nicety — and the thing that makes it safe is that
an id means the same thing under both bindings, because an id is the
sha256 of the content.

WHAT IS ASSERTED HERE, and what is not. No bucket is contacted: these
are the properties that must hold before anyone points this at real
storage — the id is the content hash, the binding is chosen by name and
an unknown one is refused, and the module imports on a machine with no
cloud SDK. Whether GCS returns the bytes is GCS's problem.
"""

from __future__ import annotations

import hashlib
import importlib
import os

import pytest


def _fresh_store_module(monkeypatch, **env):
    """Reimport the store with a given environment.

    The binding is read at import time, which is correct — the value the
    process runs with should not change under it — so a test that wants
    a different one has to reimport.
    """
    for key in ("ARTIFACT_STORE", "ARTIFACT_BUCKET", "ARTIFACT_PREFIX",
                "ARTIFACT_ROOT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import kernel.artifacts.store as store

    return importlib.reload(store)


def test_the_default_binding_is_the_filesystem(monkeypatch, tmp_path):
    """E0 stays cloud-free, so absence of configuration means local."""
    store = _fresh_store_module(monkeypatch, ARTIFACT_ROOT=str(tmp_path))
    assert store.BINDING == "filesystem"
    assert isinstance(store.get_store(), store.FilesystemStore)


def test_an_unknown_binding_is_refused_by_name(monkeypatch):
    """Not defaulted. A typo that silently falls back to the filesystem
    on a host with no shared disk writes every artifact into a container
    about to be destroyed, and the first symptom is a journal full of
    result_refs pointing at nothing."""
    store = _fresh_store_module(monkeypatch, ARTIFACT_STORE="s3")
    with pytest.raises(store.UnknownBinding) as exc:
        store.get_store()
    assert "s3" in str(exc.value)
    assert "filesystem" in str(exc.value), "the refusal should name the options"


def test_the_gcs_binding_needs_a_bucket_and_says_so(monkeypatch):
    """KeyError, deliberately. A guessed bucket name is a permission
    error at the first write, inside a leg, instead of a refusal at
    startup."""
    store = _fresh_store_module(monkeypatch, ARTIFACT_STORE="gcs")
    with pytest.raises(KeyError) as exc:
        store.get_store()
    assert "ARTIFACT_BUCKET" in str(exc.value)


def test_the_gcs_module_imports_without_the_sdk():
    """E0's image carries four packages and google-cloud-storage is not
    one of them, while E5's module-load check imports EVERY module under
    kernel/. A module-level SDK import would fail that gate on a machine
    behaving exactly as designed, reported as 'the kernel does not
    import'."""
    import kernel.artifacts.gcs as gcs

    assert hasattr(gcs, "GcsStore")
    # The name must not have been bound at module scope.
    assert "storage" not in vars(gcs), (
        "the SDK is imported at module scope; E0 cannot load this file")


def test_both_bindings_shard_an_id_the_same_way(monkeypatch, tmp_path):
    """Not cosmetic: it is what lets a bucket be rsynced from a
    filesystem root, and back, without rewriting any id."""
    store = _fresh_store_module(monkeypatch, ARTIFACT_ROOT=str(tmp_path))
    from kernel.artifacts.gcs import GcsStore

    artifact_id = hashlib.sha256(b"x").hexdigest()
    fs_tail = store.FilesystemStore(str(tmp_path))._path(artifact_id)
    fs_tail = fs_tail.replace(os.sep, "/").split(str(tmp_path).replace(os.sep, "/"))[-1]
    gcs_name = GcsStore("bucket")._name(artifact_id)
    assert fs_tail.strip("/") == gcs_name, (fs_tail, gcs_name)


def test_a_prefix_is_applied_and_normalised(monkeypatch):
    from kernel.artifacts.gcs import GcsStore

    artifact_id = hashlib.sha256(b"y").hexdigest()
    assert GcsStore("b", "/runs/").      _name(artifact_id).startswith("runs/")
    assert "//" not in GcsStore("b", "/runs/")._name(artifact_id)


def test_the_async_facade_covers_every_method():
    """A method reachable only synchronously is one a leg will call on
    the event loop, and a blocking bucket read there stops every other
    leg in the process — visible as other legs missing a heartbeat, not
    as an error in the leg that caused it."""
    import kernel.artifacts.aio as aio
    from kernel.artifacts.store import FilesystemStore

    methods = {n for n in vars(FilesystemStore)
               if not n.startswith("_") and callable(getattr(FilesystemStore, n))}
    covered = {n[1:] for n in vars(aio) if n.startswith("a") and callable(getattr(aio, n))}
    missing = methods - covered
    assert not missing, f"no async wrapper for {sorted(missing)}"


def test_no_synchronous_store_call_sits_inside_an_async_def():
    """The rule the facade exists to make keepable.

    A synchronous store method inside an `async def` blocks the whole
    event loop for the length of a network round trip. Under the
    filesystem binding it costs microseconds and nobody notices, which
    is exactly why this needs a guard rather than a habit — the defect
    is invisible until the binding changes, and then it presents as
    OTHER legs missing their heartbeat, not as anything wrong with the
    leg that caused it.

    LIMIT, stated so nobody trusts this further than it goes: it finds
    DIRECT calls only. A synchronous helper that calls the store and is
    itself awaited from a coroutine blocks the loop just as hard and is
    not caught here — `kernel/artifacts/reading.py` and
    `kernel/assistant/read.py` are in that position today.
    """
    import ast
    import io as _io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for path in list((root / "kernel").rglob("*.py")) + \
            list((root / "app").rglob("*.py")):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts or path.name == "aio.py":
            continue
        try:
            tree = ast.parse(_io.open(path, encoding="utf-8",
                                      errors="replace").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and "get_store" in ast.dump(inner.func)):
                    offenders.append(
                        f"{path.relative_to(root)}:{inner.lineno} "
                        f"in async def {node.name}")
    assert not offenders, (
        "synchronous artifact-store calls on the event loop; use "
        "kernel.artifacts.aio:\n  " + "\n  ".join(sorted(set(offenders))))
