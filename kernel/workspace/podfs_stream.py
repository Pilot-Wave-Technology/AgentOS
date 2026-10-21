"""E5 — the one podfs route whose body is not JSON.

WHY IT HAD TO BE ITS OWN ROUTE. Every other operation answers with a
JSON object, and `archive` answered with the whole tar base64'd inside
one. That is two full copies of the tree in the pod (the tar, then its
base64) and two more in the caller (the body, then the decode) -- four,
at 4/3 size, for something a `large` tier can make 50GiB. It does not
get slow, it gets OOM-killed, and an OOM is classified `error`, which
means three retries and a return to the executor at gen+1 to repair
code that was never broken. Plan 9.6.

So the tar goes down the socket as `application/octet-stream` and lands
in a file. Neither end holds it: the pod streams `git archive` through
`tarfile`'s 16KB buffer onto the wire, and the reader copies the body
into a file a megabyte at a time.

THE STATUS LINE IS WITHHELD UNTIL THE FIRST BYTE. A 200 sent before the
export starts cannot be taken back when git turns out to have no such
commit, and the caller would get an empty-but-well-formed tar -- fault
8's shape exactly, where an empty capture read as a successful one and
every criterion became `error`. Deferring it means the common failure
(a bad repo or commit, which git reports immediately) still arrives as a
JSON error on a route that has not yet committed to a body.

AND A TRUNCATED TREE MUST NOT READ AS A WHOLE ONE. Once bytes are on
the wire there is no status left to change, so completeness is carried
by the tar format's OWN end-of-archive marker: two 512-byte NUL blocks,
written only by a successful close. `treestream` deliberately abandons
its writer on failure so they are absent, and `_require_complete` here
refuses the file. A half tree would fail the criteria for files that
were never missing.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import time

from kernel.workspace.podfs import ROOT, WORKER_PORT
from kernel.workspace.podfs_auth import HEADER
from kernel.workspace.podfs_client import (WorkspaceOpFailed,
                                           WorkspaceUnreachable)

logger = logging.getLogger(__name__)

# The path this route answers on. A path and not an `op`, because the
# dispatch table in `podfs.handle` returns a dict by construction and
# this one returns a socket's worth of bytes.
PATH = "/archive"

# One read, one write. Bigger than `tarfile`'s 16KB because a raw body
# copy pays syscalls and nothing else.
CHUNK = 1 << 20

# ── THREE TIMEOUTS, BECAUSE THEY ARE THREE DIFFERENT QUESTIONS ──────────
#
# `podfs_client` has one number per operation and says why: a single
# 120s covering connect AND read meant discovering a dead pod cost two
# minutes of a pool thread, the credential sweep dialled reclaimed pods
# every four seconds, threads accumulated faster than they drained, and
# the conductor wedged for thirty minutes behind an idle event loop.
#
# A STREAMING READ IS NOT THAT QUESTION. `urlopen(timeout=N)` cannot
# split them -- it hands one number to `create_connection` and leaves it
# on the socket -- so this route uses `http.client` directly, which can:
#
#   CONNECT   short, and the same 10s as every other small op. A pod
#             that has not completed a handshake in ten seconds is gone,
#             and that is the case that emptied the pool.
#   IDLE      generous, and it is a per-`recv` timeout rather than a
#             budget: a 50GiB tree legitimately takes many minutes, and
#             what is NOT legitimate is two minutes of nothing arriving.
#   DEADLINE  a wall-clock cap on the whole body, because an idle
#             timeout alone cannot end a peer that trickles. A thread
#             must be bounded; that is what the wedge taught.
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 120
DEADLINE = 1800

# Tar's end-of-archive: two 512-byte blocks of NUL.
_END_BLOCKS = 1024
_BLOCK = 512


class _DeferredBody:
    """A writer that sends the status line on the first byte, not before."""

    def __init__(self, handler):
        self.handler = handler
        self.started = False

    def write(self, data):
        if not self.started:
            self.started = True
            self.handler.send_response(200)
            self.handler.send_header("Content-Type",
                                     "application/octet-stream")
            self.handler.end_headers()
        return self.handler.wfile.write(data)

    def flush(self):
        if self.started:
            self.handler.wfile.flush()


def respond(handler, payload: dict) -> None:
    """Stream a committed tree as this request's response body."""
    from kernel.verifier.treestream import export           # noqa: PLC0415

    body = _DeferredBody(handler)
    try:
        export(payload.get("repo") or ROOT,
               payload.get("commit") or "HEAD", body,
               prefix=payload.get("prefix") or "verify/",
               uid=int(payload.get("uid") or 0),
               exclude=payload.get("exclude") or None)
    except Exception as exc:                                # noqa: BLE001
        # NEVER A TRACEBACK ACROSS THE WIRE, the same rule `handle`
        # states: it tells the caller nothing it can act on and leaks
        # paths from a container it cannot see.
        logger.exception("podfs: streaming archive")
        if body.started:
            # The end-of-archive blocks were never written, so the
            # reader will refuse the file. There is no status left.
            return
        _json_error(handler, f"E_WORKSPACE_OP_FAILED: "
                             f"{type(exc).__name__}: {exc}")


def _json_error(handler, message: str) -> None:
    raw = json.dumps({"error": message}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def fetch(host: str, payload: dict, dst: str, *, token: str,
          deadline: float = DEADLINE) -> int:
    """Stream `/archive` into the file at `dst`. Returns bytes written."""
    conn = http.client.HTTPConnection(host, WORKER_PORT,
                                      timeout=CONNECT_TIMEOUT)
    raw = json.dumps(payload).encode()
    try:
        conn.connect()
        # The handshake is done; from here the question is liveness of a
        # transfer, not of a pod. See the block comment above.
        conn.sock.settimeout(IDLE_TIMEOUT)
        conn.request("POST", PATH, body=raw,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(raw)),
                              HEADER: token})
        response = conn.getresponse()
        if response.status != 200:
            raise WorkspaceUnreachable(
                f"the workspace at {host} REFUSED a streamed archive: "
                f"HTTP {response.status}. See kernel/workspace/"
                f"podfs_auth.py -- a read-only token is refused for "
                f"anything that writes.")
        if "json" in (response.getheader("Content-Type") or ""):
            body = json.loads(response.read() or b"{}")
            raise WorkspaceOpFailed(f"the workspace pod could not produce "
                                    f"the tree: {body.get('error')}")
        written = _drain(response, dst, deadline)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        _discard(dst)
        raise WorkspaceUnreachable(
            f"streaming the archive from {host} failed: {exc}") from exc
    finally:
        conn.close()
    _require_complete(dst, written, host)
    return written


def _drain(response, dst: str, deadline: float) -> int:
    """The body into a file, a megabyte at a time.

    `shutil.copyfileobj` is the same loop and is what this would be, but
    it has nowhere to check a wall clock -- and an unbounded copy on a
    pool thread is the wedge again, one layer out.
    """
    began, written = time.monotonic(), 0
    with open(dst, "wb") as out:
        while True:
            block = response.read(CHUNK)
            if not block:
                return written
            out.write(block)
            written += len(block)
            if time.monotonic() - began > deadline:
                raise TimeoutError(
                    f"the tree was still arriving after {deadline:.0f}s "
                    f"({written // (1024 * 1024)}MB); a peer that "
                    f"trickles must not hold a pool thread for ever")


def _require_complete(dst: str, written: int, host: str) -> None:
    """Refuse a tar that has no end-of-archive marker."""
    whole = (written >= _END_BLOCKS and written % _BLOCK == 0
             and _tail(dst) == b"\0" * _END_BLOCKS)
    if whole:
        return
    _discard(dst)
    raise WorkspaceOpFailed(
        f"the tree streamed from {host} stopped after {written} bytes "
        f"with no end-of-archive marker, so it is a PART of the "
        f"committed tree. Running the criteria against it would fail "
        f"the work for files that were never missing.")


def _tail(dst: str) -> bytes:
    with open(dst, "rb") as handle:
        handle.seek(-_END_BLOCKS, os.SEEK_END)
        return handle.read(_END_BLOCKS)


def _discard(dst: str) -> None:
    """A partial tree is never left where something could read it."""
    try:
        os.unlink(dst)
    except OSError:
        pass
