"""Run the workspace's committed setups in the executor's sandbox.

THE OTHER HALF of kernel/verifier/prepare.py, and the reason the whole
scheme converges: the sandbox and the Verifier build the SAME committed
definition, so a dependency the tree forgot to declare fails here first
— while the author is still around — instead of surfacing later as a
verification refusal that names a module and blames nobody.

ONCE PER SANDBOX, marked by a sentinel file in the workspace rather
than a table: the fact "this container ran its setups" lives and dies
with the container, and the workspace is the one place both the worker
and a replacement worker can see. `env_setup` (the executor's tool)
ignores the sentinel on purpose — its whole job is re-running after the
manifest changed.

WHAT THIS FILE DOES NOT DO IS WRITE ANYTHING DOWN. The marker, the
notes and the `stall_reason` live in `envrecord.py`, which owns the one
rule they share — only the process the workspace is on may write into
it — because this file had that rule on its success path and not on its
failure path (plan 11.4, P2). Records go through `envrecord`; do not
re-open a file in here.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time

from kernel.bridge.client import BRIDGE_HOST
from kernel.verifier.setups import from_dir, from_pod
from kernel.worker import envcheck
from kernel.worker.envrecord import (already_done, drafts, failure_note,
                                     mark_done, stalled)

logger = logging.getLogger(__name__)

# The spelling `test_pod_workspace_is_reachable` calls it by. The
# function moved to `envrecord`; the name stays reachable here so the
# move did not have to be an edit in a test file this change does not
# own.
_already_done = already_done

# E14 — THE INSTALL IS A JOB NOW, not a long call. It asked for 600s
# against a model ceiling of 120 and was refused outright; then it
# imported the model's 120 and timed out honestly instead; then it got a
# 600s ceiling of its own, which is a CLIFF rather than a fix — a
# monorepo past it reports `TimeoutError`, which reads as a broken
# manifest and is not one. `jobs_client` starts it in the pod under a
# budget the pod enforces and polls; no call here is long, and a pass
# that runs out of patience leaves the install running and re-attaches
# on the next tick instead of starting a second one.
from kernel.bridge import jobs_client

# How long ONE PASS watches the install before leaving it for the next
# tick. Not a ceiling on the install: the job keeps going in the pod.
SETUP_WAIT_S = jobs_client.DEFAULT_WAIT_S


async def _ats(conn, execution_id: int) -> list[str]:
    """Git mounts only: a dataset with a stray package.json in it is
    data, not a project, and running npm inside it helps nobody."""
    rows = await conn.fetch(
        "SELECT at, binding FROM execution_mounts WHERE execution_id = $1",
        execution_id)
    if not rows:
        return [""]
    return [r["at"] or "" for r in rows if r["binding"] == "git"]


def _line(setup: dict, run: dict) -> str:
    """One setup's result, said so the reader knows who to blame.

    THE THREE ENDINGS ARE DIFFERENT FACTS and the old single line made
    them one. "exit=1 bridge error: TimeoutError" is what parked KER25,
    and it reads as a broken manifest whether the manifest was broken,
    the install was merely slow, or the bridge was unreachable.

    AND IT NAMES THE COMMAND, which it did not. This said which MANIFEST
    matched and never what was RUN, and `pip -q` prints almost nothing,
    so a model whose environment came out wrong had no way to see why.
    The `pyproject.toml` row installed `-e .` without the dev extras for
    months: pytest then failed at config load, the report said
    `[repo:pyproject.toml] exit=0`, and the executor could only conclude
    its own repository was broken. The row is fixed, but the invisibility
    was the deeper fault -- it hides the NEXT wrong command too, and this
    function exists so "the reader knows who to blame".
    """
    label = f"[{setup['at'] or '.'}:{setup['source']}]"
    ran = (setup.get("command") or "").strip()
    # The tail, not the head: every row opens with the same
    # `.git/info/exclude` preamble, and what differs is the install.
    if len(ran) > 160:
        ran = "..." + ran[-160:]
    ran = f" ran: {ran}" if ran else "" 
    if run["state"] == "running":
        return (f"{label} still installing as {run['job']} after "
                f"{run.get('elapsed_s') or 0:.0f}s — it keeps going in the "
                f"sandbox and the next tick picks it back up")
    if run["state"] == "error":
        return f"{label} could not be run: {run.get('error')}.{ran}"
    if run.get("ended") == "budget":
        return (f"{label} was stopped after {run.get('elapsed_s') or 0:.0f}s "
                f"because it reached its budget. This is a TIMEOUT, not a "
                f"failing manifest. {(run.get('output') or '')[-400:]}")
    return (f"{label} exit={run.get('exit_code')}{ran} "
            f"{(run.get('output') or '')[-400:]}")


async def run_setups(conn, execution_id: int, workspace: str,
                     port: int, host: str = BRIDGE_HOST) -> dict | None:
    """Every discovered setup, through the bridge. None = nothing to do."""
    ats = await _ats(conn, execution_id)
    # WHERE THE TREE ACTUALLY IS. `from_dir` opens files on THIS
    # process's filesystem, which is the conductor. On Docker that is
    # also where the workspace is; on Kubernetes the claim is
    # ReadWriteOncePod and belongs to the execution's pod, so every
    # manifest read came back missing and every pod execution ran with
    # nothing installed -- silently, because "no setups found" is a
    # legitimate answer for a repository that declares none.
    remote = bool(host) and host != BRIDGE_HOST
    # BOTH BRANCHES OFF THE LOOP: `from_pod` makes one podfs round trip
    # per manifest it looks for, and `from_dir` walks the tree.
    setups = await asyncio.to_thread(
        (lambda: from_pod(host, workspace, ats, execution_id=execution_id))
        if remote else (lambda: from_dir(workspace, ats)))
    if not setups:
        # No committed definition anywhere: the scout reads the repo
        # like a new teammate and DRAFTS one into SETUP_SUGGESTED.md.
        # A draft, never an act -- the executor reviews and commits it,
        # and only the committed text is ever built. `drafts` declines
        # on a pod and says so; that guard used to be written out here,
        # and writing it out is how the failure path came to be missing
        # it.
        await drafts(execution_id, workspace, host, ats,
                     "no manifest or .agentos/setup.sh found in this mount")
        return None
    outputs, failed, pending = [], None, None
    # ONE PATIENCE FOR THE WHOLE PASS, not one per setup: three manifests
    # waiting SETUP_WAIT_S each would hold the leg lock for three times
    # as long as anybody agreed to.
    deadline = time.monotonic() + SETUP_WAIT_S
    for index, s in enumerate(setups):
        prefix = f"cd {shlex.quote(s['at'])} && " if s["at"] else ""
        run = await jobs_client.run_job(
            prefix + s["command"],
            # `host` for the same reason every other bridge call takes
            # one: on Kubernetes the shim is in the execution's pod, and
            # the default localhost is the conductor.
            host=host, port=port,
            # THE KEY IS WHAT SURVIVES A RESTART, and it is why this can
            # give up watching without giving up the install. Measured on
            # execution 200, when the old blocking call timed out at 31s
            # and the caller retried: a SECOND pip against the same tree.
            # Starting with the same key while the first is running
            # re-attaches to it instead.
            key=f"env-setup-{execution_id}-{index}",
            wait_s=max(5.0, deadline - time.monotonic()))
        outputs.append(_line(s, run))
        if run["state"] == "running":
            pending = s
            break
        if run["state"] == "error" or run.get("exit_code") != 0:
            failed = s
            break
    # DID IT BUILD WHAT IT SAID IT WOULD? Only worth asking once the
    # pass has actually finished: a setup still installing has not had
    # its chance yet, and a failed one already has a better report.
    if failed is None and pending is None:
        note = await _bins_note(setups, host, port)
        if note:
            outputs.append(note)

    return {"setups": [f"{s['at'] or '.'}:{s['source']}" for s in setups],
            "state": "running" if pending else "done",
            "ok": failed is None and pending is None,
            "output": "\n".join(outputs)}


async def _bins_note(setups, host: str, port) -> str:
    """The `envcheck` line for this pass, or "" -- never raises.

    A probe that fails tells us nothing about the environment, so it
    says nothing. Reporting "could not check" beside a working setup
    would be noise on every run of every repo whose bridge blinked.
    """
    command = envcheck.probe_command(setups)
    if not command:
        return ""
    try:
        # NO KEY: a re-attachable probe would be re-attached to on the
        # next pass and answer about the previous tree. `state` is
        # "exited", "running" or "error"; only the first says anything.
        run = await jobs_client.run_job(command, host=host, port=port,
                                        wait_s=15.0)
        if run.get("state") != "exited":
            return ""
        return envcheck.line(*envcheck.read_probe(run.get("output") or ""))
    except Exception as exc:                                  # noqa: BLE001
        logger.debug("bin probe failed (%s); nothing is claimed", exc)
        return ""


async def _report_failure(conn, execution_id: int, workspace: str,
                          host: str, result: dict) -> None:
    """The three records a failed setup leaves, all of them guarded.

    SAID WHERE SOMEBODY LOOKS. The console log is not the model's
    context and not the person's page; a setup that failed only there is
    a setup nobody knows failed. `stall_reason` renders on the
    Executions board, the note and the scout's draft are where the model
    reads files, and `env_setup` re-runs after the manifest is fixed.

    ALL THREE GO THROUGH `envrecord`, which is where the "may this
    process write into this workspace" question is answered from `host`.
    Plan 6: the obvious guard to copy here references `remote`, a local
    of `run_setups` -- a `NameError` on the failure path is worse than
    the wrong write it replaces, because the executor then learns
    nothing at all about why its environment is missing.
    """
    await stalled(conn, execution_id, result["output"])
    # A failing setup gets the scout too: the draft sits beside the
    # failure note, so the fix is one review away.
    await drafts(execution_id, workspace, host,
                 [entry.split(":", 1)[0] for entry in result["setups"]],
                 "the committed setup failed: " + result["output"][-300:])
    await failure_note(execution_id, workspace, host, result["output"])


async def ensure_setup(conn, row: dict) -> dict | None:
    """The worker's once-per-sandbox pass. Returns an action dict or None.

    Failure is REPORTED, never fatal: the leg still runs, the model sees
    a workspace whose environment is missing, and the setup log is in
    the action for the person watching. Blocking the leg would turn a
    typo in requirements.txt into a story nobody can steer.
    """
    workspace = row.get("workspace_path") or ""
    container = row.get("container_id") or ""
    port = row.get("bridge_port")
    if not (workspace and container and port):
        return None
    host = row.get("workspace_host") or BRIDGE_HOST
    if await already_done(workspace, container, host, row["id"]):
        return None
    try:
        result = await run_setups(conn, row["id"], workspace, port, host)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("env setup for %s failed to run: %s", row["id"], e)
        return {"execution_id": row["id"], "action": "setup_error",
                "detail": f"{type(e).__name__}: {e}"}
    if result is not None and result["state"] == "running":
        # NOT DONE, SO NOT MARKED. The install is still going in the
        # sandbox under its own budget; the marker means "this sandbox
        # has had its setups run" and writing it now would mean nobody
        # ever asks again. The next tick re-enters here, re-attaches to
        # the same job by key, and keeps watching.
        logger.info("execution %s: setup still running (%s)", row["id"],
                    ", ".join(result["setups"]))
        return {"execution_id": row["id"], "action": "setup_running",
                "detail": result["output"][-200:]}
    await mark_done(workspace, container, host, port,
                    result["output"] if result else "no setups discovered")
    if result is None:
        return None
    if not result["ok"]:
        await _report_failure(conn, row["id"], workspace, host,
                              result)
    logger.info("execution %s: sandbox setup ok=%s (%s)", row["id"],
                result["ok"], ", ".join(result["setups"]))
    return {"execution_id": row["id"],
            "action": "setup" if result["ok"] else "setup_failed",
            "detail": ", ".join(result["setups"])
                      + ("" if result["ok"] else
                         " — " + result["output"][-200:])}
