"""E5 — the pod's tree crosses the wire as bytes, not as base64 JSON.

WHAT WAS THERE. `op_archive` ran `git archive` into memory, base64'd the
result into a JSON object, and `podfs_client.archive` base64-decoded the
whole body back. Four copies of the tree between two processes, at 4/3
size, for something a `large` tier can make 50GiB -- and the OOM that
follows is classified `error`, which loops back to the executor to
repair code that was never broken. Plan 9.6.

THREE PROPERTIES ARE ASSERTED HERE, and each is load-bearing:

  bounded     a 32MB tree crosses with a peak of a few MB on the reading
              side. The number is not the point; the SHAPE is -- a peak
              that does not scale with the tree.
  scoped      section 5.5 requires the verification pod to pull with a
              READ-ONLY token, because podfs has write operations and a
              criterion able to call `publish` could rewrite the tree it
              is checking. The narrow token pulls and is refused
              everything else.
  honest      a tree that stopped halfway is REFUSED rather than handed
              to the criteria, which would fail the work for files that
              were never missing. Completeness rides tar's own
              end-of-archive blocks, written only by a successful close.

Nothing here reaches a cluster: podfs is served on a loopback port in
this process, which is the same `Handler` the pod runs.
"""
from __future__ import annotations

import http.client
import os
import shutil
import subprocess
import threading
import tracemalloc
from http.server import ThreadingHTTPServer

import pytest

from kernel.verifier.tarslice import names
from kernel.workspace import podfs, podfs_auth, podfs_client, podfs_stream
from kernel.workspace.podfs_client import (WorkspaceOpFailed,
                                           WorkspaceUnreachable)

EXECUTION = 4242
BIG_MB = 32
ALLOWANCE = 6 * 1024 * 1024

needs_git = pytest.mark.skipif(shutil.which("git") is None,
                               reason="the export shells out to git")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("tree")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "t@example.invalid"),
                       ("user.name", "test")):
        subprocess.run(["git", "-C", str(path), "config", key, value],
                       check=True)
    block = os.urandom(1024 * 1024)
    with open(path / "payload.bin", "wb") as handle:
        for _ in range(BIG_MB):
            handle.write(block)
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "c1"],
                   check=True)
    return str(path)


@pytest.fixture
def served(monkeypatch):
    """The pod's own `Handler`, on a loopback port this test can dial."""
    monkeypatch.setenv(podfs_auth.ENV_VAR, podfs_auth.derive(EXECUTION))
    server = ThreadingHTTPServer(("127.0.0.1", 0), podfs.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    # The port is a module constant on both clients; in the pod it is
    # 9101 and here it is whatever the kernel handed out.
    monkeypatch.setattr(podfs_stream, "WORKER_PORT", port)
    monkeypatch.setattr(podfs_client, "WORKER_PORT", port)
    yield "127.0.0.1"
    server.shutdown()


def peak_of(work):
    tracemalloc.start()
    try:
        return work(), tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# ── bounded ──────────────────────────────────────────────────────────────

@needs_git
def test_the_tree_arrives_as_a_file_and_never_as_a_value(served, repo,
                                                         tmp_path):
    """THE DELIVERABLE. The reader copies the body a megabyte at a time
    into a file; nothing on this side ever holds the tree."""
    dst = str(tmp_path / "tree.tar")
    written, peak = peak_of(
        lambda: podfs_client.archive_to_file(served, repo, "HEAD", dst,
                                             execution_id=EXECUTION))
    assert written > BIG_MB * 1024 * 1024
    assert written == os.path.getsize(dst)
    assert {"verify/a.py", "verify/payload.bin"} <= names(dst)
    assert peak < ALLOWANCE, f"{peak // (1024 * 1024)}MB resident"


@needs_git
def test_the_uid_and_the_exclusions_still_apply_across_the_wire(served, repo,
                                                                tmp_path):
    """The criteria run as a different uid from the executor on purpose,
    and a tree they cannot write fails most check runners for a reason
    that has nothing to do with the work."""
    dst = str(tmp_path / "tree.tar")
    podfs_client.archive_to_file(served, repo, "HEAD", dst, uid=65534,
                                 exclude=["payload.bin"],
                                 execution_id=EXECUTION)
    assert "verify/payload.bin" not in names(dst)
    with __import__("tarfile").open(dst) as handle:
        assert all(m.uid == 65534 for m in handle)


# ── scoped ───────────────────────────────────────────────────────────────

def test_the_narrow_token_pulls_a_tree(served, repo, tmp_path):
    """Section 5.5's preferred shape: the verification pod dials podfs
    itself, holding a token derived from the execution's own."""
    dst = str(tmp_path / "ro.tar")
    podfs_client.archive_to_file(served, repo, "HEAD", dst,
                                 execution_id=EXECUTION, read_only=True)
    assert names(dst)


def test_the_narrow_token_cannot_publish_the_branch_it_is_checking(served,
                                                                   repo):
    """THE REASON THE SCOPE EXISTS. `publish` pushes with the story's git
    identity and `credentials` places a lease; a criterion holding the
    full token could commit as the story it was asked to judge."""
    with pytest.raises(WorkspaceUnreachable) as caught:
        podfs_client.call(served, "publish", execution_id=EXECUTION,
                          read_only=True, root=repo, mounts=[])
    assert "403" in str(caught.value)
    assert "read this workspace and not change it" in str(caught.value)


def test_a_token_for_another_execution_is_not_recognised_at_all(served,
                                                                repo):
    """401 and 403 are different diagnoses. An unrecognised token learns
    nothing about which operations exist."""
    with pytest.raises(WorkspaceUnreachable) as caught:
        podfs_client.call(served, "digest", execution_id=EXECUTION + 1,
                          root=repo, mounts=[])
    assert "401" in str(caught.value)


def test_the_narrow_token_cannot_be_walked_back_to_the_full_one():
    """Derived FROM the full token rather than from the signing key, so
    the worker can verify it without a second secret, without the key,
    and without `k8s_spec` and `_safe_env` being changed together --
    which is the two-list arrangement that let the proxy variables be
    fixed twice and still reach the model."""
    full = podfs_auth.derive(EXECUTION)
    narrow = podfs_auth.derive_read_only(EXECUTION)
    assert narrow != full
    assert podfs_auth.read_only_of(full) == narrow
    assert podfs_auth.permits("readonly", "archive")
    assert not podfs_auth.permits("readonly", "publish")
    assert not podfs_auth.permits("something-new", "archive")


# ── honest ───────────────────────────────────────────────────────────────

@needs_git
def test_a_commit_the_pod_cannot_archive_is_an_error_not_an_empty_tree(
        served, repo, tmp_path):
    """The status line is withheld until the first byte, so the common
    failure still arrives as a JSON error rather than as a 200 carrying
    a well-formed empty tar -- fault 8's shape exactly, where an empty
    capture read as a successful one and every criterion became
    `error`."""
    dst = str(tmp_path / "missing.tar")
    with pytest.raises(WorkspaceOpFailed) as caught:
        podfs_client.archive_to_file(served, repo, "0" * 40, dst,
                                     execution_id=EXECUTION)
    assert "could not produce the tree" in str(caught.value)
    assert not os.path.exists(dst)


def test_a_tree_that_stopped_halfway_is_refused_and_deleted(tmp_path,
                                                            monkeypatch):
    """Once bytes are on the wire there is no status left to change, so
    completeness rides tar's own end-of-archive blocks -- written only
    by a successful close, and withheld by `treestream` when the export
    dies. A part of a tree handed to the criteria would fail the work
    for files that were never missing."""
    server = _truncating_server()
    monkeypatch.setattr(podfs_stream, "WORKER_PORT",
                        server.server_address[1])
    dst = str(tmp_path / "half.tar")
    with pytest.raises(WorkspaceOpFailed) as caught:
        podfs_stream.fetch("127.0.0.1", {"op": "archive"}, dst, token="t")
    server.shutdown()
    assert "no end-of-archive marker" in str(caught.value)
    assert not os.path.exists(dst)


def _truncating_server() -> ThreadingHTTPServer:
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):                                  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"\x01" * 4096)   # a tar, cut off mid-member

        def log_message(self, fmt, *a):                     # noqa: A003
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── the timeouts, which are three questions and not one ──────────────────

def test_the_handshake_is_bounded_short_and_the_transfer_is_not(monkeypatch):
    """`podfs_client` has one number per op because a single 120s
    covering connect AND read meant discovering a dead pod cost two
    minutes of a pool thread -- the credential sweep dialled reclaimed
    pods every four seconds, the pool emptied, and the conductor wedged
    for thirty minutes behind an idle event loop.

    A STREAMING READ IS NOT THAT QUESTION, and `urlopen` cannot ask both:
    it hands one number to `create_connection` and leaves it on the
    socket. So this route uses `http.client`, gives the handshake the
    same short timeout every small op gets, and re-arms the socket for
    the transfer once there is a peer on the other end.
    """
    seen = {}

    class Recorder:
        def __init__(self, host, port, timeout=None):
            seen["connect_timeout"] = timeout
            self.sock = self

        def settimeout(self, value):
            seen["idle_timeout"] = value

        def connect(self):
            seen["connected"] = True

        def request(self, *a, **k):
            raise OSError("far enough")

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", Recorder)
    with pytest.raises(WorkspaceUnreachable):
        podfs_stream.fetch("10.0.0.1", {"op": "archive"}, "unused",
                           token="t")
    assert seen["connect_timeout"] == podfs_stream.CONNECT_TIMEOUT == 10
    assert seen["idle_timeout"] == podfs_stream.IDLE_TIMEOUT
    # The transfer's per-read allowance is generous where the handshake's
    # is not, and a wall clock still bounds the whole body -- an idle
    # timeout alone cannot end a peer that trickles, and an unbounded
    # copy on a pool thread is the wedge again one layer out.
    assert podfs_stream.IDLE_TIMEOUT > podfs_stream.CONNECT_TIMEOUT
    assert podfs_stream.DEADLINE >= podfs_stream.IDLE_TIMEOUT
