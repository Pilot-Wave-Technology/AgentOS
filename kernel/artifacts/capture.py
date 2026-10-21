"""Take a directory out of a pod that is about to stop existing.

    python -m kernel.artifacts.capture --dir D --signal F [--timeout S]

WHY THIS EXISTS. The environment replay installs the executor's recorded
packages once, in a networked container, and carries the result to
criteria that have no network of their own. On Docker that last step is
`GET /containers/{id}/archive` before the container is destroyed. A Job's
pod cannot be read after it exits, and there is no Kubernetes equivalent
— so the directory has to LEAVE rather than be collected, and the only
place it can go is the artifact store.

THE SHAPE IT RUNS IN. Two containers in one pod sharing an emptyDir at
the captured path:

    work      the contract's image, running the install or the setup
              script, its command wrapped so it writes its exit status
              to a signal file when it finishes
    capture   this, which waits for that file, tars the directory, puts
              it in the store and prints the id

A sidecar rather than something appended to the work container's own
command, because the work container is the CONTRACT'S image: it has no
kernel/, no cloud SDK, and no reason to. Everything this needs lives in
our image, which is the right place for it.

THE ID COMES BACK THROUGH THE LOG, because a content-addressed store
means the caller cannot know it in advance — the id IS the hash of bytes
that do not exist yet. `MARKER` is what the driver greps for.

WHAT A HOSTILE SETUP SCRIPT GETS FROM THIS, stated rather than left to
be discovered. The work container runs repository-authored code and
shares this pod's service account, so it can reach the metadata server
and mint the same bucket-write token. It cannot forge a tree with it:
`store.get` re-hashes every read, so an object replaced under its own
name is refused at the point of use. It can write junk objects, which
costs storage and nothing else.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tarfile
import time

# What the driver greps the log for. Distinctive on purpose: a setup
# script's own output is on the WORK container's stream, not this one,
# but a marker that could occur in ordinary text is a marker that will.
MARKER = "AGENTOS_CAPTURE_ID"

POLL_S = 0.5


def wait_for(signal_path: str, timeout_s: float) -> str | None:
    """Block until the work container says it is done. Its status, or None.

    THE SIGNAL IS A FILE and not the process table: `shareProcessNamespace`
    is False in every pod this kernel builds, deliberately — sharing it
    would let one container read another's /proc/<pid>/environ — so
    there is nothing to wait on but the filesystem they share.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(signal_path):
            try:
                with open(signal_path, encoding="utf-8") as handle:
                    return handle.read().strip()
            except OSError:
                return ""
        time.sleep(POLL_S)
    return None


def archive(directory: str) -> bytes:
    """The directory as a tar rooted at its own basename.

    THE SAME SHAPE DOCKER'S ARCHIVE ENDPOINT RETURNS — capturing
    `/tmp/verify` gives members under `verify/`. Everything downstream
    reads those names: `only_new` subtracts the slice's own entries and
    `rebase_deps_tar` re-roots them, and both would silently produce
    nothing against a differently rooted tar.
    """
    root = os.path.basename(os.path.normpath(directory))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if os.path.isdir(directory):
            absolute: list[str] = []
            tar.add(directory, arcname=root, recursive=True,
                    filter=lambda ti: _defer_absolute_link(ti, absolute))
            _add_dereferenced(tar, directory, root, absolute)
    return buf.getvalue()


def _defer_absolute_link(info, absolute: list):
    """Hold back symlinks with an absolute target. See `_add_dereferenced`."""
    if info.issym() and os.path.isabs(info.linkname):
        absolute.append(info.name)
        return None
    return info


def _add_dereferenced(tar, directory: str, root: str, names: list) -> None:
    """Re-add the held-back links as the FILES THEY POINT AT.

    MEASURED ON EXECUTION 209. The criterion unpacks this tar with
    `tarfile`'s `data` filter, which refuses an absolute symlink:

        AbsoluteLinkError: 'verify/repos/<repo>/.venv/bin/python3' is a
        link to an absolute path

    and `python -m venv` ALWAYS writes `bin/python3` as a link to the
    absolute path of its base interpreter. So every captured `.venv` was
    unextractable, and the whole env_setup -> verify path could not
    complete for any story with a manifest.

    WHY HERE AND NOT AT THE EXTRACTION. `data` refuses these for good
    reason: the tar is ours but it is built FROM A REPOSITORY, and a
    link to an absolute path followed by a write through it is the
    classic way out of an extraction root. Relaxing the filter would
    buy this one case by weakening the boundary for hostile content.
    Dereferencing here costs a few megabytes and keeps the filter
    strict.

    It is also MORE CORRECT. A symlink to `/usr/local/bin/python3`
    resolves only if the criterion's image happens to carry the same
    interpreter at the same path; the contract chooses that image, so
    that is an assumption and not a fact. A copied binary works
    wherever the tree lands -- which is what `python -m venv --copies`
    would have produced, applied to every setup rather than only the
    ones we author.

    A link we cannot resolve is DROPPED, not restored: it was already
    broken inside the container that made it, and carrying a dangling
    absolute link forward only moves the failure into the criterion.
    """
    parent = os.path.dirname(os.path.normpath(directory))
    for name in names:
        # `name` is rooted at `root`; map it back to a real path.
        real = os.path.join(parent, name)
        target = os.path.realpath(real)
        if not os.path.isfile(target):
            # A directory link or a dangling one. Neither can be copied
            # sensibly, and both are already unusable downstream.
            continue
        try:
            info = tar.gettarinfo(target, arcname=name)
            with open(target, "rb") as handle:
                tar.addfile(info, handle)
        except OSError:
            # Unreadable is the same outcome as dangling: say nothing
            # into the tar rather than fail the whole capture over one
            # entry the tree may not even use.
            continue


def capture(directory: str, signal: str, timeout_s: float) -> int:
    from kernel.artifacts.store import get_store

    status = wait_for(signal, timeout_s)
    if status is None:
        # NOT a silent empty capture. A caller handed an empty tar with
        # exit 0 would replay an environment with nothing in it and
        # report criteria failing on missing imports as if the work were
        # wrong.
        print(f"capture: the work container never signalled within "
              f"{timeout_s:.0f}s; nothing was captured", file=sys.stderr)
        return 1

    if not os.path.isdir(directory):
        # Said out loud, and still an artifact: a setup that failed
        # before creating its directory is a real outcome, and the
        # caller reads the work container's exit status for the verdict.
        print(f"capture: {directory} does not exist; capturing nothing",
              file=sys.stderr)

    blob = archive(directory)
    artifact_id = get_store().put(blob)
    print(f"{MARKER} {artifact_id}")
    print(f"capture: {len(blob)} bytes from {directory} "
          f"(work exited {status or '?'})", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kernel.artifacts.capture")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--signal", required=True)
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args(argv[1:])
    try:
        return capture(args.dir, args.signal, args.timeout)
    except Exception as exc:                                # noqa: BLE001
        print(f"capture: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
