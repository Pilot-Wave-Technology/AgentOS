"""The PATH the executor's own setups created, per mount.

WHAT WAS WRONG, measured on execution 219 and true since the registry
was written. `setups.REGISTRY` declares, per manifest, the directories
whose executables a check may call BARE -- its own comment says so:

    `bins` are the in-tree directories whose executables the checks may
    call bare (`pytest`, not `.venv/bin/pytest`).

Only one room honoured it. The Verifier turns `bins` into
`mounts_env[at]["prepend"]` and `criterion_env` puts the target mount's
first. The executor discovered the same setups through the same
functions -- `envsetup.py` imports `from_dir`/`from_pod` from that very
registry -- ran their commands, and THREW THE BINS AWAY. `_tool_bash`
runs `subprocess.run(["bash", "-c", cmd], env=_safe_env())` with one
fixed environment.

So bare `pytest` was not found in the model's own sandbox, and
`.venv/bin/pytest` was -- because the executor's setups install in-tree.
The model wrote the form that worked WHERE IT WAS STANDING. That check
then reached the verification pod, where the environment lives on
`volume.ENV` so it survives the tree being replaced each round, and
`.venv/bin/pytest` does not exist there:

    `cd repos/... && .venv/bin/pytest test_app.py`: command not found

Three rounds of `error`, and the claim went back to the executor to fix
work that was never broken.

WHY THIS IS NOT "TELL THE MODEL TO WRITE `pytest`". Until the bare form
works in the sandbox, that instruction asks for a command the model
cannot run, cannot test, and has every reason to distrust. The
convention has to be true before it can be required.

ONE SCOPING RULE, NOT TWO. `criterion_env.environment_for` already
decides which mount's executables go first, from the `cd <mount> &&`
prefix every check carries, and it exists because one global PATH let
the first repository's `.venv` shadow the second's. This module supplies
the same shape of input so the executor can reuse that function rather
than grow a second answer to the same question.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _prepend_for(at: str, bins) -> list:
    """In-tree bin directories, relative to the workspace root.

    THE EXECUTOR'S LAYOUT, NOT THE VERIFIER'S, and they genuinely
    differ. `prepare_script.bin_paths` answers `/verify/env/<at>/<bin>`
    because the prepare Job installs onto the volume's env side; the
    executor's setups run with cwd at the mount and land in the tree, so
    the same `bins` mean `<at>/<bin>` under the workspace. Same
    declaration, two roots -- which is exactly why a check must name
    neither and call the tool by name.
    """
    return [f"{at}/{b}" if at else b for b in bins]


def from_setups(discovered: list) -> dict:
    """`{at: {"prepend": [...], "env": {}}}` from discovered setups.

    Shaped like the Verifier's `prepare_volume._mounts_env` so
    `criterion_env.environment_for` accepts it unchanged.

    `env` is empty here on purpose. The Verifier also folds in
    `.agentos/env`, which it has already read into memory while hashing
    the environment's identity; the executor has not, and opening it
    per leg to add variables the sandbox mostly already has is a cost
    with no measured need. The PATH is the part that was missing.
    """
    by_at: dict = {}
    for s in discovered or ():
        by_at.setdefault(s.get("at") or "", []).extend(s.get("bins") or ())
    return {at: {"prepend": _prepend_for(at, dict.fromkeys(bins)), "env": {}}
            for at, bins in by_at.items() if bins}


async def for_execution(conn, execution_id: int, workspace: str,
                        host: str = "") -> dict:
    """The mounts_env for this run's sandbox. `{}` on any doubt.

    NEVER RAISES, and never blocks a leg. This buys a PATH entry; a
    workspace that cannot be walked, a driver that will not answer, a
    mount table that is empty -- every one of them means the model gets
    the environment it got before this existed, which is a `bash` that
    works and a check that may not port. Failing the leg over a
    convenience would be strictly worse than the bug it fixes.
    """
    if not workspace:
        return {}
    try:
        import asyncio                                       # noqa: PLC0415

        from kernel.verifier.setups import from_dir, from_pod  # noqa: PLC0415
        from kernel.workspace.mounts import mounts_for        # noqa: PLC0415

        mounts = await mounts_for(conn, execution_id)
        ats = sorted({(m.at or "") for m in mounts}) or [""]

        # THE SAME BRANCH `envsetup` TAKES, and for the same reason:
        # `from_dir` opens files on THIS process's filesystem. In the
        # execution pod that is the tree, correctly. Anywhere the tree
        # belongs to another pod it is the wrong disk, and `from_pod`
        # asks the one that has it.
        from kernel.bridge.client import BRIDGE_HOST         # noqa: PLC0415

        remote = bool(host) and host != BRIDGE_HOST
        discovered = await asyncio.to_thread(
            (lambda: from_pod(host, workspace, ats,
                              execution_id=execution_id))
            if remote else (lambda: from_dir(workspace, ats)))
        return from_setups(discovered)
    except Exception as exc:                                 # noqa: BLE001
        logger.warning("execution %s: could not resolve the sandbox PATH "
                       "(%s); bare tool names may not resolve",
                       execution_id, exc)
        return {}


def with_mount_path(command: str, mounts_env: dict) -> str:
    """`command`, with the target mount's bin directories on PATH.

    THE TARGET IS THE `cd` PREFIX, decided by `criterion_env` and not
    re-derived here. Its docstring records why: one global PATH made the
    first repository's `.venv` shadow the second's, and the convention
    every check follows -- `cd <mount> && ...` -- names which one is
    meant.

    RELATIVE PATHS, RESOLVED AGAINST THE CWD THE COMMAND SETS. The
    prepends are workspace-relative (`repos/x/.venv/bin`) and the
    command has just `cd`-ed into `repos/x`, so they are exported
    absolute against the workspace root the sandbox already runs in --
    `$PWD` would be the mount, one level too deep.
    """
    if not mounts_env or not (command or "").strip():
        return command
    try:
        from kernel.verifier.criterion_env import (          # noqa: PLC0415
            environment_for)

        _env, prepend = environment_for(command, mounts_env)
    except Exception:                                        # noqa: BLE001
        return command
    if not prepend:
        return command
    root = "${AGENTOS_WORKSPACE_ROOT:-/workspace}"
    absolute = ":".join(f"{root}/{p}" for p in prepend.split(":") if p)
    return f'export PATH="{absolute}:$PATH"; {command}'
