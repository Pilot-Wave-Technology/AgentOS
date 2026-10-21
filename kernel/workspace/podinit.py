"""Compose the workspace from inside the pod that will use it.

    python -m kernel.workspace.podinit

THE CONDUCTOR CANNOT DO THIS ANY MORE, and that is the whole of P3/D1.
`prepare_workspace` clones into the filesystem the conductor is running
on, which is right on Docker — the sandbox mounts the same named volume
— and impossible on Kubernetes, where the workspace is a
ReadWriteOncePod claim belonging to the execution's own pod. The access
mode exists so the API server REFUSES a second writer; the conductor
could not mount it even as root, and when it tried anyway the symptom
was `[Errno 13] Permission denied: '/workspace'` every tick.

So composition moves to the only process that can reach the claim.

THE SAME CODE, NOT A SECOND COPY. This calls `CompositeDriver.acquire`
— the function the conductor calls on Docker. A pod-side reimplementation
of cloning would be a second declaration of what a workspace is, and the
two would drift the first time a binding was added. What differs between
the substrates is WHERE the code runs, which is exactly what this module
is; what a workspace IS stays in one place.

WHY AN initContainer AND NOT THE WORKER ON STARTUP. A pod is not Ready
until its init containers exit 0, so a clone that fails is a pod that
never starts — and `await_ready` already gates dispatch on that. The
alternative is a run that dispatches into an empty directory, which
`kernel/worker/runner.py` records as a live failure: a transient 404
from the credential broker failed a clone, the run dispatched anyway,
and the executor wrote a ten-step plan to create from scratch the
branches the engine had already cut.

IDEMPOTENT, BECAUSE THIS IS ALSO THE RESUME PATH. A cold-parked run
(PARKED_COLD) loses its pod and keeps its claim — `destroy()` deletes
only the pod, because "the claim exists to survive" preemption. The
replacement pod runs this again over a populated volume, and
`CompositeDriver.acquire` adopts every mount already present rather than
re-cloning: `GitDriver` short-circuits on `.git` existing. Re-cloning
here would discard precisely the uncommitted work a resume exists to
keep.
"""

from __future__ import annotations

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

# The mount list, as JSON, on the init container's environment. Carried
# rather than re-derived: `mounts_for` reads four tables and joins
# git_nodes to resolve the engine's feature branch, and giving a pod the
# database to ask again would hand an execution container the one
# credential the whole design keeps away from it.
MOUNTS_ENV = "AGENTOS_WORKSPACE_MOUNTS"

# Named here, beside MOUNTS_ENV and for the same reason: the manifest
# builder sets it and this module reads it, so the spelling is one fact.
# Two spellings of an environment variable fail exactly as silently as
# two declarations of a URL -- which is the bug this variable exists to
# fix. The direction of the import (manifest -> podinit) is the existing
# one; reversing it for this constant would be a cycle.
STORY_ENV = "AGENTOS_STORY_ID"

# B1: spelled out rather than hidden in an environ.get default, so the
# value this process runs with is visible in the code.
_ROOT_ENV = os.environ.get("AGENTOS_WORKSPACE_ROOT")
ROOT = _ROOT_ENV or "/workspace"


class ComposeFailed(RuntimeError):
    """The workspace is not what was granted. The pod must not start."""


def mounts_from(raw: str) -> tuple:
    """Rebuild the Mount tuple the conductor serialised.

    Refuses malformed JSON rather than composing an empty workspace: a
    run with no mounts and a run whose mount list could not be read look
    identical from inside the pod, and only one of them should proceed.
    """
    from kernel.workspace.mounts import Mount

    if not (raw or "").strip():
        return ()
    try:
        rows = json.loads(raw)
    except ValueError as e:
        raise ComposeFailed(
            f"{MOUNTS_ENV} is not JSON ({e}). An unreadable mount list "
            f"is not an empty one, and composing nothing would start a "
            f"run against a workspace it was not granted.") from e
    if not isinstance(rows, list):
        raise ComposeFailed(f"{MOUNTS_ENV} must be a list, got {type(rows).__name__}")
    return tuple(
        Mount(at=r.get("at", ""), binding=r.get("binding", "none"),
              source=r.get("source") or "", ref=r.get("ref") or "",
              writable=bool(r.get("writable")), kind=r.get("kind") or "",
              resource_id=r.get("resource_id") or "")
        for r in rows)


def incomplete(root: str, mounts) -> list:
    """Git mounts that are not real checkouts. The same check the
    conductor applies on Docker, applied where the tree now lives.

    A HALF-BUILT TREE IS THE SAME LIE AS NO TREE. `acquire` returning is
    not evidence: a clone can fail per-mount and leave the directory
    there. Reporting completeness from inside the pod is what lets the
    pod's exit status carry it.
    """
    missing = []
    for mount in mounts:
        if mount.binding != "git":
            continue
        at = os.path.join(root, mount.at) if mount.at else root
        if not os.path.isdir(os.path.join(at, ".git")):
            missing.append(mount.at or ".")
    return missing


def _make_group_writable(root: str) -> None:
    """Give the shared group write on everything under `root`.

    THE SETGID BIT ON DIRECTORIES TOO, so a file the sandbox creates
    later inherits the group rather than the creator's primary one --
    otherwise the worker, which commits, cannot write what the sandbox
    made and the problem reappears pointing the other way.

    Best effort per entry: a tree we cannot chmod is worse than one we
    can, but refusing to start the pod over one unreadable path would
    turn a degraded workspace into no workspace.
    """
    for here, dirs, files in os.walk(root):
        for name in dirs:
            path = os.path.join(here, name)
            try:
                os.chmod(path, os.stat(path).st_mode | 0o020 | 0o010 | 0o2000)
            except OSError:
                logger.debug("could not chmod %s", path)
        for name in files:
            path = os.path.join(here, name)
            try:
                mode = os.stat(path).st_mode
                # +w for the group, and +x only where the owner has it:
                # making every file executable would change what the
                # tree MEANS, and a checked-in script keeps its bit.
                os.chmod(path, mode | 0o020 | (0o010 if mode & 0o100 else 0))
            except OSError:
                logger.debug("could not chmod %s", path)


def compose(root: str, raw_mounts: str, execution_id: str = "",
            story_id: str = "") -> str:
    """Establish every granted mount under `root`. Returns the root."""
    # 0o002: files 0664, directories 0775, for everything created from
    # here on. See `_make_group_writable` for why the sandbox needs it.
    os.umask(0o002)
    from kernel.workspace import object_fetch
    from kernel.workspace.composite import CompositeDriver
    from kernel.workspace.driver import WorkspaceSpec

    # THE POD HAS NO CONTROL PANEL TO INJECT A FETCHER. `CopyinDriver`
    # resolves a `gs://` document through a fetcher the host registers,
    # because "the kernel must not import the control panel". Here there
    # is no host, so the kernel registers its own — see object_fetch.
    object_fetch.register()

    mounts = mounts_from(raw_mounts)
    os.makedirs(root, exist_ok=True)
    # `story_id` IS LOAD-BEARING, not metadata. The git credential
    # helper refuses to build itself without one, so a spec missing it
    # clones private repos unauthenticated and the pod dies here.
    CompositeDriver().acquire(WorkspaceSpec(
        execution_id=int(execution_id) if str(execution_id).isdigit() else 0,
        root=root, mounts=mounts, story_id=story_id))

    # THE BRIEFING, WRITTEN WHERE THE TREE IS. On Docker the conductor
    # writes this after cloning; on a pod the conductor never sees the
    # tree, so the write has to happen here or not at all -- and until
    # now it was "not at all". Two checkouts of one repository look
    # identical from `ls`, and an executor that guesses which it may
    # edit is silent until the commit lands on the wrong branch.
    #
    # NOT FATAL. A workspace without its README is worse than one with
    # it and far better than a pod that refuses to start; the mounts are
    # real either way.
    try:
        from kernel.workspace.briefing import write_workspace_md
        write_workspace_md(root, mounts)
    except Exception:                                       # noqa: BLE001
        logger.warning("WORKSPACE.md not written", exc_info=True)

    # THE SANDBOX HAS TO BE ABLE TO EDIT WHAT WE JUST CLONED.
    #
    # `fsGroup` on the pod chowns the volume to the shared group and
    # makes it group-writable AT MOUNT TIME. This container clones
    # AFTERWARDS, as the worker's uid with the default umask, so every
    # file it creates lands 0644 and every directory 0755 -- owner
    # 65531, group 65532, and the sandbox is 65532. Read, no write.
    #
    # Measured on a live pod, which is how this was found:
    #
    #   /workspace        drwxrwsr-x  root  65532    <- fsGroup, writable
    #   repos/<repo>/     drwxr-sr-x  65531 65532    <- not writable
    #   README.md         -rw-r--r--  65531 65532    <- not writable
    #
    # An ERRAND did not notice: it writes its report at the workspace
    # root, which fsGroup had already made group-writable. A code story
    # cannot edit a single file, and the executor -- correctly -- parks
    # and asks a person to fix the permissions, which is a thing no
    # person can do from a chat card.
    #
    # BOTH HALVES. The umask covers everything created from here on,
    # including a later checkout; the walk covers what the clone already
    # wrote before we got here. Neither alone is enough.
    _make_group_writable(root)

    missing = incomplete(root, mounts)
    if missing:
        raise ComposeFailed(
            f"the workspace is missing {len(missing)} checkout(s): "
            f"{', '.join(missing)}. A run dispatched against a half-built "
            f"tree invents the work it cannot find.")
    logger.info("workspace composed at %s: %d mount(s)", root, len(mounts))
    return root


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raw = os.environ[MOUNTS_ENV] if MOUNTS_ENV in os.environ else ""
    eid = os.environ["AGENTOS_EXECUTION_ID"] \
        if "AGENTOS_EXECUTION_ID" in os.environ else ""
    sid = os.environ[STORY_ENV] if STORY_ENV in os.environ else ""
    try:
        compose(ROOT, raw, eid, sid)
    except Exception as e:                                  # noqa: BLE001
        # NON-ZERO, so the pod never becomes Ready. The message is the
        # only thing a person will see -- `kubectl describe` shows the
        # init container's last log -- so it says what was not
        # established rather than a traceback.
        print(f"workspace not composed: {e}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
