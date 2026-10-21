"""What a setup pass WRITES DOWN, and the one rule all of it obeys.

THE SEAM. `envsetup.py` runs a workspace's committed setups; this module
owns every record that outlives the pass -- the done-marker a later tick
reads, the notes the model reads, the `stall_reason` the Executions
board renders. They are separated because they have a shared rule that
the running half does not, and the rule was being restated once per
writer and then forgotten on one of them (plan 11.4, P2):

    ONLY THE PROCESS THE WORKSPACE IS ON MAY WRITE INTO IT.

On Kubernetes the workspace claim is ReadWriteOncePod and belongs to the
execution's pod; `open(os.path.join(workspace, ...), "w")` on the
conductor therefore writes to the CONDUCTOR's filesystem. It fails
silently when nothing is there -- and when something is (a same-named
tree left by an earlier local run) it writes a real file into the wrong
workspace, where the next reader believes it. That is the shape of the
failure this module exists to make impossible to repeat: `run_setups`
had the guard on its no-manifest path and `ensure_setup`'s FAILURE path
did not, so the two writes a failed setup makes were the unguarded ones.

THE OTHER RULE, and it is the same one volume.stale() states from the
other side: an ABSENT record is the safe direction. `stale()` treats a
missing stamp as stale so an unidentifiable environment gets rebuilt; a
missing done-marker costs a repeated setup, which is waste. A PARTIAL
record is the unsafe direction in both places -- and `open(path, "w")`
truncates before it writes, so an error halfway leaves exactly that:
a zero-byte marker that `already_done` reads as "this sandbox has had
its setups run", for the life of the container. Nothing here writes in
place; do not "simplify" `_atomic` back into an `open(..., "w")`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex

from kernel.bridge.client import BRIDGE_HOST, call_bridge
from kernel.bridge.limits import HARNESS_FOREGROUND_S, bridge_timeout

logger = logging.getLogger(__name__)

NL = chr(10)


def on_conductor(host: str) -> bool:
    """Is the workspace on THIS process's filesystem.

    THE ONE SPELLING of a fact that had three: `remote` in `run_setups`,
    and `not host or host == BRIDGE_HOST` written out inline in both
    marker functions. It is a function of `host` and nothing else, which
    is what lets `ensure_setup` ask it too -- plan 6 notes that `remote`
    is a local of `run_setups`, so the naive way to guard the failure
    path (reference `remote` there) is a `NameError` on the one path
    whose whole job is to explain why setup failed.
    """
    return not host or host == BRIDGE_HOST


def _sentinel(workspace: str, container_id: str) -> str:
    return os.path.join(workspace, f".agentos-setup-{container_id[:12]}")


def _atomic(path: str, text: str) -> bool:
    """Write `text` at `path`, or leave NOTHING there. True on success.

    The temp file is in the same directory on purpose: `os.replace` is
    atomic within a filesystem and not across one, and a workspace is
    routinely a mount. On failure the partial temp goes and the real
    path is untouched -- so the reader sees the previous record or no
    record, never half of this one.
    """
    tmp = f"{path}.{os.getpid()}.part"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())        # a crash must not leave 0 bytes
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


async def already_done(workspace: str, container: str, host: str,
                       execution_id: int) -> bool:
    """Has this sandbox had its setups run.

    THE MARKER LIVES IN THE WORKSPACE, so asking about it means asking
    the machine the workspace is on. Checked with `os.path.exists` on
    the conductor, which on Kubernetes is a path that does not exist
    there -- so the answer was always "no" and the setups re-ran on
    EVERY TICK: a `pip install` per tick, for the life of the run,
    through the sandbox's bridge. Worse than not running them at all,
    and equally silent.
    """
    marker = _sentinel(workspace, container)
    if on_conductor(host):
        return os.path.exists(marker)
    from kernel.workspace import podfs_client                # noqa: PLC0415
    try:
        # OFF THE EVENT LOOP. `podfs_client.call` is a synchronous
        # `urlopen` with a 120s timeout, and this runs on EVERY leg on a
        # pod substrate -- so one slow workspace pod froze every
        # coroutine in the conductor for two minutes. Same fault as the
        # Kubernetes calls, in a module the first audit did not cover.
        _, problem = await asyncio.to_thread(
            podfs_client.read_file, host, workspace, marker,
            execution_id=execution_id)
        return not problem
    except Exception:                                       # noqa: BLE001
        # Unreachable is not "done". Re-running a setup is wasteful;
        # skipping one because the worker hiccupped leaves a run with no
        # environment and no record of why.
        return False


async def mark_done(workspace: str, container: str, host: str, port: int,
                    text: str) -> None:
    """Record that they ran, where the workspace can see it.

    Through the BRIDGE on a pod, not podfs: podfs is deliberately
    read-only -- the only thing permitted to put bytes in a workspace is
    the sandbox itself. The bridge runs in that sandbox and mounts the
    same claim.

    THE CONTENT IS INFORMATIONAL and a pod gets only the marker, not the
    log: the text can be a hundred lines of pip output and shell-quoting
    it into a `bash` call to preserve a convenience is how a quoting bug
    reaches a workspace. The log is already in the action dict, which is
    what the Executions board renders.

    WRITTEN AFTER A FAILURE TOO, and that is deliberate: the marker says
    "this sandbox ran its setups", not "they worked". A failing manifest
    that cleared the marker would re-run its `pip install` every tick
    for the life of the run -- the exact fault `already_done` exists to
    describe -- and the way back is `env_setup`, which ignores the
    marker by design.
    """
    marker = _sentinel(workspace, container)
    if on_conductor(host):
        if not _atomic(marker, text):
            # Nothing was written, and that is the safe direction: the
            # next tick re-runs the setups rather than skipping them on
            # the strength of a truncated file.
            logger.warning("setup marker not written for %s", container)
        return
    try:
        touch = {"command": f"touch {shlex.quote(marker)}",
                 "timeout": HARNESS_FOREGROUND_S}
        await call_bridge("bash", touch,
                          f"setup-marker-{container[:12]}", port=port,
                          host=host, timeout=bridge_timeout("bash", touch))
    except Exception:                                       # noqa: BLE001
        # A missing marker costs a repeated setup, not a wrong one.
        logger.warning("setup marker not written for %s", container)


async def drafts(execution_id: int, workspace: str, host: str, mounts,
                 reason: str) -> None:
    """Ask the scout to draft a setup for each mount. Never on a pod.

    The scout reads the tree AND WRITES INTO IT, and podfs offers no
    write -- the one thing that may put bytes in a workspace is the
    sandbox, through the bridge. Both callers reach this: the
    no-manifest path, which has declined on a pod since the pod
    substrate landed, and the setup-FAILED path, which did not. Same
    writer, same filesystem, one guard, and it is here rather than at
    the call sites so the third caller cannot forget it.

    Skipped LOUDLY: a draft is a convenience, and running the setups
    that do exist is the part that matters.
    """
    if not on_conductor(host):
        # NAMED BY EXECUTION, not by path: a pod's workspace path is
        # `/workspace` on every one of them, so the path alone tells a
        # reader of the conductor's log nothing about which run this is.
        logger.info("execution %s: no setup draft; the scout writes and a "
                    "pod workspace is not on this filesystem (%s)",
                    execution_id, reason[:80])
        return
    try:
        from kernel.worker.scout import suggest_setup    # noqa: PLC0415
        for at in mounts:
            await suggest_setup(workspace, "" if at == "." else at, reason)
    except Exception:                                       # noqa: BLE001
        logger.exception("scout failed; no suggestion written")


async def failure_note(execution_id: int, workspace: str, host: str,
                       output: str) -> None:
    """SETUP_FAILED.md, where the model reads files. Never on a pod.

    The model's copy of the failure. It is one of the two writes plan
    11.4 calls unguarded: on a pod it either vanishes into an OSError
    nobody sees, or lands in a stale same-named tree on the conductor
    where a later scout `_gather` reads it back as this workspace's.
    A pod's model still gets the failure -- in the action detail and in
    `stall_reason`, which are not filesystem writes.
    """
    if not on_conductor(host):
        logger.info("execution %s: no SETUP_FAILED.md; a pod workspace is "
                    "not on this filesystem, and the failure is in "
                    "stall_reason", execution_id)
        return
    body = NL.join([
        "# Workspace setup failed", "",
        "Fix the manifest it names, then call the `env_setup` tool to "
        "re-apply.", "", "```", output, "```", ""])
    if not _atomic(os.path.join(workspace, "SETUP_FAILED.md"), body):
        logger.warning("execution %s: SETUP_FAILED.md not written",
                       execution_id)


async def stalled(conn, execution_id: int, output: str) -> None:
    """Say it where somebody looks: `stall_reason` on the Executions row.

    The console log is not the model's context and not the person's
    page; a setup that failed only there is a setup nobody knows failed.

    GUARDED ON RUNNING, like `intake.apply_cancellation` and every
    `stall_reason` writer in `conductor/admit.py`. This pass can watch
    an install for SETUP_WAIT_S, and a run cancelled in that window is
    already terminal -- painting a stall on it puts "It is stalled"
    (answerer/record.py, assistant/progress.py) on work that ended, with
    no edge left that would ever clear it. The worker loop only ever
    hands this function RUNNING rows, so the guard costs nothing when
    the row has not moved.
    """
    await conn.execute(
        "UPDATE executions SET stall_reason = $2, updated_at = now() "
        "WHERE id = $1 AND state = 'RUNNING'",
        execution_id, ("workspace setup failed -- fix the manifest and "
                       "call env_setup: " + output[-300:])[:500])
