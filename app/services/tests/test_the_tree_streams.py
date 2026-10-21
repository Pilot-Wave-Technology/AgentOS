"""E5 — the committed tree is never held whole, and that is ASSERTED.

THE DEFECT THESE CLOSE. `archive_committed_tree` returned `bytes`, and
`tarslice`, `surface` and `prepare` all parsed those bytes in memory.
At the 45MB measured on execution 210 that is fine. A `large` tier has
a 50GiB volume and an `xlarge` 100GiB, and at that size the conductor
does not slow down -- it is OOM-killed. The kill is classified
`infra_error`, correctly, because a ceiling nobody declared says nothing
about the work; but `error` means three identical retries and then a
return to the executor at gen+1, where the model is asked to repair code
that was never broken. That is the loop measured on 205, 207, 208 and
209, and plan 9.6 records that it arrives structurally the first time a
tier above `small` runs a real suite.

SO BOUNDED MEMORY IS THE DELIVERABLE, and a deliverable nobody measures
is a deliverable that regresses. `tracemalloc` watches the peak across
each operation: a 32MB tree moved with a peak under a few MB is a
pipeline whose cost does not scale with the tree, and the same test
fails loudly the day somebody reintroduces a `.read()`.

WHAT IS NOT PROVED HERE. 32MB is not 50GiB. What the number shows is
the SHAPE -- peak independent of input -- which is the property that
makes the large case survivable; the absolute ceiling belongs to a
cluster and is named in the report rather than asserted here.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tracemalloc

import pytest

from kernel.verifier import surface, tree
from kernel.verifier.tarslice import names, restamp, slice_subtree
from kernel.verifier.tarstream import Sink, opened

# Big enough that holding it is unmistakable against the allowance
# below, small enough that a test suite can afford to build it.
BIG_MB = 32

# What a streaming pipeline is allowed to have resident. `tarfile`
# copies through 16KB and the raw body copy through 1MB, so a few MB is
# headroom rather than a tight fit -- and it is 10x below the point
# where holding one copy of the tree would show.
ALLOWANCE = 6 * 1024 * 1024

needs_git = pytest.mark.skipif(shutil.which("git") is None,
                               reason="the export shells out to git")


@pytest.fixture(scope="module")
def big_repo(tmp_path_factory):
    """A committed tree with one large file in it."""
    repo = tmp_path_factory.mktemp("big")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@example.invalid"),
                       ("user.name", "test")):
        subprocess.run(["git", "-C", str(repo), "config", key, value],
                       check=True)
    block = os.urandom(1024 * 1024)
    with open(repo / "payload.bin", "wb") as handle:
        for _ in range(BIG_MB):
            handle.write(block)
    (repo / "test_a.py").write_text("assert True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "big"],
                   check=True)
    return str(repo)


def peak_of(work):
    """The high-water mark of this process while `work` runs."""
    tracemalloc.start()
    try:
        result = work()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def a_tar(**files) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as out:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            out.addfile(info, io.BytesIO(body))
    return buf.getvalue()


# ── the export ───────────────────────────────────────────────────────────

@needs_git
def test_the_export_lands_on_disk_without_the_tree_entering_memory(
        big_repo, tmp_path):
    """THE DELIVERABLE. `git archive` streams through a 16KB buffer into
    a file, so the peak is a buffer and not a tree."""
    dst = str(tmp_path / "tree.tar")
    _, peak = peak_of(lambda: tree.stream_committed_tree(big_repo, "HEAD",
                                                         dst))
    assert os.path.getsize(dst) > BIG_MB * 1024 * 1024
    assert peak < ALLOWANCE, f"{peak // (1024 * 1024)}MB resident"


@needs_git
def test_the_streamed_tar_is_the_one_the_old_path_produced(big_repo,
                                                           tmp_path):
    """Same members, same ownership, same exclusions, same injections.

    The byte form is what every existing caller still uses, so the two
    have to be the same archive or this refactor is a behaviour change
    wearing a performance change's clothes.
    """
    kwargs = dict(uid=65534, exclude=["payload.bin"],
                  inject={"tests/held.py": "assert True"})
    dst = str(tmp_path / "streamed.tar")
    tree.stream_committed_tree(big_repo, "HEAD", dst, **kwargs)
    in_memory = tree.archive_committed_tree(big_repo, "HEAD", **kwargs)
    assert sorted(names(dst)) == sorted(names(in_memory))
    with opened(dst) as streamed, opened(in_memory) as held:
        assert [(m.name, m.uid, m.mode) for m in streamed] == \
               [(m.name, m.uid, m.mode) for m in held]


@needs_git
def test_a_failed_export_leaves_no_file_to_mistake_for_a_tree(big_repo,
                                                              tmp_path):
    """`close()` writes tar's end-of-archive blocks, so a destination
    finalised after its source died is a VALID archive missing files --
    fault 10's shape, a failure that arrives looking like a result. The
    criteria would run against it and fail the work for it."""
    dst = str(tmp_path / "nothing.tar")
    with pytest.raises(tree.TreeExportError):
        tree.stream_committed_tree(big_repo, "0" * 40, dst)
    assert not os.path.exists(dst)


# ── the consumers ────────────────────────────────────────────────────────

@needs_git
def test_slicing_a_tree_on_disk_holds_neither_end_of_it(big_repo, tmp_path):
    """`prepare` slices one mount out of the tree per setup. It read the
    whole tar in and built the whole slice in memory: two copies, for an
    operation that is a header edit and a byte copy."""
    src = str(tmp_path / "tree.tar")
    tree.stream_committed_tree(big_repo, "HEAD", src)
    out = str(tmp_path / "slice.tar")
    _, peak = peak_of(lambda: slice_subtree(src, "verify/", "", out=out))
    assert "verify/payload.bin" in names(out)
    assert peak < ALLOWANCE, f"{peak // (1024 * 1024)}MB resident"


@needs_git
def test_restamping_a_tree_on_disk_holds_neither_end_of_it(big_repo,
                                                           tmp_path):
    src = str(tmp_path / "tree.tar")
    tree.stream_committed_tree(big_repo, "HEAD", src)
    out = str(tmp_path / "owned.tar")
    _, peak = peak_of(lambda: restamp(src, 65534, out=out))
    with opened(out) as tf:
        assert all(m.uid == 65534 for m in tf)
    assert peak < ALLOWANCE, f"{peak // (1024 * 1024)}MB resident"


@needs_git
def test_the_check_surface_hashes_without_holding_the_files_it_hashes(
        big_repo, tmp_path):
    """The surface's own list used to carry every matched file's whole
    CONTENT so it could sort before hashing -- so `check_surface` of
    `**` was a way to run the conductor out of memory with the defence
    that exists to catch a rewritten test."""
    src = str(tmp_path / "tree.tar")
    tree.stream_committed_tree(big_repo, "HEAD", src)
    digest, peak = peak_of(lambda: surface.hash_tar(src, ["**"],
                                                    prefix="verify/"))
    assert digest.startswith("sha256:")
    assert peak < ALLOWANCE, f"{peak // (1024 * 1024)}MB resident"


def test_the_surface_hash_is_bit_identical_to_the_one_approvals_carry():
    """`check_surface_hash` is stored on contracts approved before E5.
    A hash that moved would refuse every one of them, which is the
    CODE_SURFACE_CHANGED refusal firing on work nobody touched."""
    tar = a_tar(**{"verify/a/x.py": b"hello", "verify/b/y.py": b"world"})
    assert surface.hash_tar(tar, ["*.py"], prefix="verify/") == (
        "sha256:40b1a2857c962e851ec8d1ed82191803c885c129fa5a6ab87ae7cd5"
        "d839ffebe")


def test_bytes_in_and_bytes_out_is_unchanged_for_every_caller(tmp_path):
    """Docker has no PVC and `console/contract.py` hashes a tar it just
    built. A flag day across every caller is how a refactor this size
    acquires its own defect."""
    tar = a_tar(**{"verify/a/x.py": b"one", "verify/b/y.py": b"two"})
    assert isinstance(slice_subtree(tar, "verify/", "a"), bytes)
    assert names(slice_subtree(tar, "verify/", "a")) == {"verify/a/x.py"}
    path = str(tmp_path / "t.tar")
    with open(path, "wb") as handle:
        handle.write(tar)
    assert names(path) == names(tar)
    assert surface.hash_tar(path, ["*.py"]) == surface.hash_tar(tar, ["*.py"])


def test_a_sink_refuses_to_hand_back_a_tar_it_never_closed():
    """The end-of-archive blocks are written on close, so a value taken
    inside the `with` is a tar no reader accepts."""
    sink = Sink(None)
    with pytest.raises(RuntimeError):
        _ = sink.result
