"""E5 — a tar is a PLACE, not a value.

`archive_committed_tree` returned `bytes` and every consumer below it
took `bytes`: `tarslice` sliced them, `surface` hashed them, `prepare`
spliced them. That is fine at the 45MB measured on execution 210 and it
is a wall at the 50GiB a `large` tier's volume can hold. It does not
degrade -- the process is killed, the OOM is classified `error` (which
is right: a ceiling nobody declared says nothing about the work), and
`error` means three retries and then a return to the executor at gen+1,
where the model is asked to repair code that was never broken. That is
the loop measured on 205, 207, 208 and 209, arriving structurally the
first time a tier above `small` runs a real suite. Plan section 9.6.

SO EVERY READER TAKES A PATH OR BYTES, AND EVERY WRITER CAN TARGET A
PATH. `tarfile` already streams: iterating a TarFile yields one header
at a time, and `addfile` copies through a 16KB buffer rather than a
`read()`. Nothing here is clever -- the whole change is refusing to
hold the middle of the pipe in memory.

BYTES STAY LEGAL, deliberately. `console/contract.py` hashes a tar it
just built, the corpus validator passes literals, and the Docker
substrate has no PVC to land a file on. A flag day across every caller
is how a refactor of this size acquires its own defect; the byte form
is the same code path with `io.BytesIO` at both ends.
"""

from __future__ import annotations

import contextlib
import io
import os
import tarfile

# What one `read` moves when this module copies by hand. `tarfile` uses
# 16KB internally; this is for the raw copies (an HTTP body, a pipe)
# where bigger blocks cost nothing and syscalls do.
CHUNK = 1 << 20


def _is_bytes(source) -> bool:
    return isinstance(source, (bytes, bytearray, memoryview))


@contextlib.contextmanager
def opened(source):
    """A tar open for reading. A path is NOT loaded, only opened.

    `tarfile.open(name)` seeks per member, so the index is built from
    headers and the file data never enters this process unless somebody
    asks for a member.
    """
    if _is_bytes(source):
        handle = tarfile.open(fileobj=io.BytesIO(bytes(source)), mode="r")
    else:
        handle = tarfile.open(os.fspath(source), mode="r")
    try:
        yield handle
    finally:
        handle.close()


class Sink:
    """Where a rewritten tar goes: a path, or memory when `out` is None.

    The two-step shape (`sink = Sink(out)` ... `sink.result`) exists
    because the result is only true after the TarFile is closed -- the
    end-of-archive blocks are written on close, and a `getvalue()` taken
    inside the `with` returns a tar no reader will accept.
    """

    def __init__(self, out=None):
        self.out = out
        self._buffer: io.BytesIO | None = None
        self._result = None

    @contextlib.contextmanager
    def open(self):
        if self.out is None:
            self._buffer = io.BytesIO()
            handle = tarfile.open(fileobj=self._buffer, mode="w")
        else:
            handle = tarfile.open(os.fspath(self.out), mode="w")
        try:
            yield handle
        except BaseException:
            # A FAILED REWRITE LEAVES NOTHING, not a short tar. `close()`
            # writes the end-of-archive blocks, so a destination closed
            # after its source died is a VALID archive missing files --
            # which the criteria would run against and fail the work for.
            # Fault 10's shape: a failure that arrives looking like a
            # result. The file goes; `result` stays unset.
            handle.close()
            if self.out is not None:
                with contextlib.suppress(OSError):
                    os.unlink(os.fspath(self.out))
            raise
        handle.close()
        self._result = (self._buffer.getvalue() if self._buffer is not None
                        else os.fspath(self.out))

    @property
    def result(self):
        """`bytes` for an in-memory sink, the path for a file one."""
        if self._result is None:
            raise RuntimeError("the sink's tar has not been closed yet; "
                               "its end-of-archive blocks are unwritten")
        return self._result


def copy_member(dst, src, member) -> None:
    """One member, header and data, from `src` into `dst`.

    `addfile` streams the payload through `tarfile`'s own copy buffer,
    so a 2GB member costs 16KB of process memory. Written once because
    six call sites spelled it identically and a seventh would have
    spelled it `read()`.
    """
    dst.addfile(member, src.extractfile(member) if member.isfile() else None)


def digest_of(handle, hasher) -> bytes:
    """`hasher` over a member's bytes, WITHOUT holding the member.

    `surface.hash_tar` accumulated every matched file's whole body in a
    list so it could sort before hashing. A check surface of
    `tests/**` over a monorepo is the tree, and sorting it required
    holding it. The sort only needs the PATHS, so the bodies collapse to
    32 bytes each here.
    """
    for block in iter(lambda: handle.read(CHUNK), b""):
        hasher.update(block)
    return hasher.digest()
