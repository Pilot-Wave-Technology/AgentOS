"""E5 — `git archive` straight into a file, never through `bytes`.

`archive_committed_tree` ran `subprocess.run(capture_output=True)`, which
holds the WHOLE tar in the parent's memory, and then rebuilt it through
two more `io.BytesIO` to re-own and exclude. Three copies of the tree,
resident at once, in a container capped at 1 CPU.

Here the pipe IS the source. `tarfile.open(mode="r|")` reads a
non-seekable stream one header at a time, `addfile` copies each payload
through a 16KB buffer, and the destination is a file on the verification
volume (kernel/verifier/volume.py). Peak memory is one member's header
plus a buffer, whatever the tree weighs.

WHY THE TRANSFORM CANNOT BE SKIPPED. `uid` re-owns every entry, because
the criteria run as a different uid from the executor on purpose and
pytest alone wants to write `__pycache__`. `exclude` drops path
prefixes. Both are per-member header edits, which is exactly what a
streaming rewrite is good at -- there is no reason this ever needed the
tree in memory.

AN INCOMPLETE ARCHIVE MUST NOT LOOK FINISHED. `close()` writes a tar's
end-of-archive blocks, so closing a destination whose source died
produces a VALID, SHORT tar -- fault 10's shape, a failure that arrives
looking like a result, and here it would hand the criteria a tree with
files missing and let them fail the work for it. So git's returncode is
checked while the destination is still open, and the destination is
finalised only on the success path.
"""

from __future__ import annotations

import contextlib
import io
import logging
import subprocess
import tarfile

from kernel.verifier.tarstream import Sink, copy_member

logger = logging.getLogger(__name__)


def write_injected(dst, inject: dict | None, prefix: str, uid: int) -> None:
    """F4's held-out set, written INTO an open archive.

    AFTER the repository's own entries, so a held-out check overrides a
    file of the same name if the executor ever guessed one into
    existence. One implementation because three paths add them -- the
    local export, the pod fetch, and the append onto a streamed file --
    and three that agree until one is edited is the bug class this
    refactor keeps finding.
    """
    for path, body in sorted((inject or {}).items()):
        data = body.encode("utf-8") if isinstance(body, str) else body
        info = tarfile.TarInfo(f"{prefix}{path.lstrip('/')}")
        info.size = len(data)
        info.mode = 0o600
        info.uid = info.gid = uid
        dst.addfile(info, io.BytesIO(data))


def transform(src, dst, prefix: str, uid: int,
              exclude: list[str] | None) -> int:
    """Copy every member across, re-owned and filtered. Returns drops."""
    dropped = 0
    skip = tuple(f"{prefix}{p.lstrip('/')}" for p in (exclude or []))
    for member in src:
        if skip and member.name.startswith(skip):
            dropped += 1
            continue
        member.uid = member.gid = uid
        member.uname = member.gname = ""
        if member.isdir():
            member.mode |= 0o700
        elif member.isfile():
            member.mode |= 0o600
        copy_member(dst, src, member)
    return dropped


@contextlib.contextmanager
def _onto_stream(out):
    """A tar written onto an already-open stream, ABANDONED on failure.

    Not closed when the body raises: `close()` is what appends the
    end-of-archive blocks, and on a socket there is nothing to delete
    afterwards -- withholding them is the only way the reader can tell a
    truncated tree from a finished one. `podfs_stream` checks for them.
    """
    handle = tarfile.open(fileobj=out, mode="w|")
    yield handle
    handle.close()


def export(repo: str, commit: str, out=None, prefix: str = "verify/",
           uid: int = 0, exclude: list[str] | None = None,
           inject: dict | None = None):
    """`commit` as a tar at `out`. `bytes` when `out` is None.

    `out` may be None (bytes), a path, or an open binary stream -- the
    last is how the podfs route writes an archive onto the wire without
    the pod holding it either.
    """
    from kernel.verifier.tree import TreeExportError        # noqa: PLC0415

    proc = subprocess.Popen(
        ["git", "-C", repo, "archive", f"--prefix={prefix}",
         "--format=tar", commit],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sink = None if hasattr(out, "write") else Sink(out)
    destination = _onto_stream(out) if sink is None else sink.open()
    try:
        # THE SOURCE OPENS FIRST. `tarfile` reads the first header when
        # it opens a stream, so a git that failed outright raises here,
        # before anything has been created or a status line sent.
        with tarfile.open(fileobj=proc.stdout, mode="r|") as src, \
                destination as dst:
            dropped = transform(src, dst, prefix, uid, exclude)
            if proc.wait() != 0:
                raise TreeExportError(
                    f"git archive {commit[:12]} of {repo} failed after "
                    f"{dropped} exclusion(s): {_stderr(proc)}")
            write_injected(dst, inject, prefix, uid)
    except tarfile.TarError as exc:
        proc.kill()
        raise TreeExportError(
            f"git archive {commit[:12]} of {repo} produced no usable "
            f"tar: {_stderr(proc)} {exc}") from exc
    finally:
        if proc.stdout is not None and not proc.stdout.closed:
            proc.stdout.close()
        proc.poll()
    if dropped:
        logger.info("excluded %d path(s) from the export", dropped)
    if inject:
        logger.info("injected %d held-out file(s)", len(inject))
    return sink.result if sink is not None else None


def _stderr(proc) -> str:
    if proc.stderr is None:
        return ""
    with contextlib.suppress(ValueError, OSError):
        return proc.stderr.read().decode(errors="replace").strip()
    return ""


def append_injected(path: str, inject: dict | None, prefix: str,
                    uid: int) -> None:
    """Add the held-out set to a tar ALREADY on disk, without rewriting.

    The streamed fetch lands the pod's archive as a file; the held-out
    checks are conductor-side by construction (F4: they are "absent from
    the executor entirely", and the execution's pod is the executor's).
    `tarfile`'s append mode seeks past the end-of-archive blocks and
    writes after them, so this costs the size of the injected files and
    not the size of the tree -- reading 50GiB back to add four test
    files would give away everything E5 exists to win.
    """
    if not inject:
        return
    with tarfile.open(path, "a") as dst:
        write_injected(dst, inject, prefix, uid)
