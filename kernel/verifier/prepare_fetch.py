"""E15 — the init container that puts the committed tree on the volume.

WHY AN init CONTAINER AND NOT THE PREPARE CONTAINER ITSELF. The prepare
container runs the CONTRACT's image (defence 1, plan 5.12): it has no
`kernel/`, no guarantee of `curl`, and no business holding a workspace
token -- the one piece of model-adjacent code in this Job is the
repository's own `.agentos/setup.sh`, which runs there. So the fetch
runs first, in the KERNEL's image, and hands the install a directory
instead of a credential. `k8s_bridge` makes the same argument for the
bridge shim and reaches the same shape.

IT ORDERS THE TWO STEPS FOR FREE. An init container must exit 0 before
the next container starts, so a tree that could not be pulled is a Job
that never runs an install -- rather than one that installs against an
empty directory and reports a repository with no manifests.

STREAMED, NEVER HELD (plan 5.5, E5). `podfs_stream.fetch` copies the
body into a file a megabyte at a time and refuses one with no
end-of-archive marker, and the unpack below reads that file one member
at a time. Peak memory is a buffer, whatever the tree weighs -- which is
the property that stops a `large` tier's 50GiB volume becoming an OOM,
an `error`, three retries, and a return to the executor to repair code
that was never broken (plan 9.6).

EVERY LINE GOES TO STDOUT, AND IT SAYS WHEN IT STARTS AND WHEN IT STOPS
(plan section 13). A container that logs only on failure leaves a
silence identical to one that never ran, and that silence is what turned
"(no pod found; output unavailable)" into an afternoon.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shutil
import sys
import tarfile

from kernel.verifier import prepare_source, volume

logger = logging.getLogger(__name__)


def _reset_tree() -> None:
    """An EMPTY `TREE`, because a stale round's files are invisible.

    The environment is cached across rounds and the tree deliberately is
    not (volume.py). Unpacking over yesterday's tree would leave a file
    the commit DELETED sitting where a check can import it -- the same
    direction as 5.9's stale `.venv`, where a check passes that should
    fail.
    """
    shutil.rmtree(volume.TREE, ignore_errors=True)
    os.makedirs(volume.TREE, exist_ok=True)


def _relative(name: str) -> str | None:
    """A member's path under `TREE`, or None when it is not ours.

    The archive is rooted at `prepare_source.PREFIX` because `podfs`
    refuses an empty prefix, so the prefix is stripped here rather than
    reproduced in the layout -- `volume.TREE` is where the criteria look
    and it has no `verify/` in it.
    """
    clean = posixpath.normpath(name)
    root = posixpath.normpath(prepare_source.PREFIX)
    if clean == root:
        return None
    if not clean.startswith(root + "/"):
        return None
    return clean[len(root) + 1:]


def unpack(tar_path: str) -> int:
    """One streamed archive onto `volume.TREE`. Returns members written.

    `filter="data"` is not decoration. The tar is produced by `git
    archive` in the EXECUTOR's pod, so its contents are whatever the
    model committed: the data filter refuses absolute paths, `..`
    escapes, device nodes and setuid bits, which is the difference
    between unpacking a tree and letting a commit write anywhere on this
    volume -- including over `volume.ENV`, one directory up.
    """
    written = 0
    with tarfile.open(tar_path, "r") as src:
        for member in src:
            rel = _relative(member.name)
            if not rel:
                continue
            member.name = rel
            src.extract(member, volume.TREE, filter="data")
            written += 1
    return written


def seed(source: dict) -> int:
    """Pull every mount and lay the tree out. Returns members written.

    AND IT DOES NOT MAKE E16's `node_modules` LINK, deliberately. The
    prepare script wipes `volume.ENV` on the very next line -- 5.9's
    rule, so a removed dependency cannot survive into the new build --
    so a link made here would be DANGLING by the time the install runs,
    and `cp -R` would then copy a symlink pointing at the directory it
    is about to create. `env_link.shell`, called after the install,
    is the prepare side's link; `env_link.link` is for the OTHER seeding
    side, the Deployment pushing a tree into the warm pod between
    generations, where the environment is not being rebuilt.

    ONE STREAM PER MOUNT, and the mount's `at` is in the archive's own
    prefix, so a two-repo workspace reassembles into the same shape the
    criteria will run against. `archive_mounts` -- the op that composes
    the whole workspace in one call -- returns base64 inside a JSON
    object, which is four copies of the tree between two processes; that
    is the ceiling E5 exists to remove and is not usable here.
    """
    from kernel.workspace.podfs_stream import fetch          # noqa: PLC0415

    _reset_tree()
    total = 0
    for mount in source["mounts"]:
        at = mount.get("at") or ""
        payload = prepare_source.request(mount, source["uid"])
        logger.info("agentos-prepare-fetch: pulling %s from %s",
                    at or ".", source["host"])
        size = fetch(source["host"], payload, prepare_source.STAGE,
                     token=source["token"])
        try:
            members = unpack(prepare_source.STAGE)
        finally:
            # THE STAGE FILE IS THE TREE AGAIN. Leaving it would double
            # every mount's footprint on a disk sized for one copy, and
            # the next mount would overwrite it anyway -- so the leak is
            # only ever the LAST mount's, which is the one nobody
            # notices until a volume is full.
            _discard(prepare_source.STAGE)
        total += members
        logger.info("agentos-prepare-fetch: %s -> %s (%d members, %dKB)",
                    at or ".", volume.TREE, members, size // 1024)
    return total


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def main() -> int:
    """SAYS WHAT PHASE IT IS IN, at both ends. Plan section 13."""
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        stream=sys.stdout)
    logger.info("agentos-prepare-fetch: start")
    try:
        source = prepare_source.read()
        written = seed(source)
    except Exception as exc:                                # noqa: BLE001
        # NAMED, NOT TRACED. This container's exit status is what stops
        # the install from running against an empty tree, and the reader
        # of that failure needs the sentence rather than the frames --
        # `podfs_stream` and `prepare_source` both raise with the
        # sentence already written.
        logger.exception("agentos-prepare-fetch: FAILED")
        logger.info("agentos-prepare-fetch: stop (failed: %s: %s)",
                    type(exc).__name__, exc)
        return 1
    logger.info("agentos-prepare-fetch: stop (%d members on %s)",
                written, volume.TREE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
