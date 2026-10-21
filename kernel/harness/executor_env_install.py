"""`env_install` — the one door into the environment that RECORDS.

The Verifier reproduces what is written here and nothing else, so this
module owns the whole of that promise: what a manager may be, how a
package name reaches a shell intact, which command each manager runs,
how long it is allowed to take, what gets stored for the replay, and
what the model is told about the parts a replay cannot carry.

WHY IT IS ITS OWN MODULE AND NOT IN `executor_tools.py`, which is where
its own docstring and that file's header both say it belongs. Two
reasons, and the second is the operative one:

  - `executor_tools.py` is 130 code lines and this is 74. Together they
    are over the house ceiling, which is the thing wave 4 is fixing.
  - It is not a peer of `env_setup`. `env_setup` re-applies what the
    committed tree already declares and has nothing to record;
    this one is the recording, and the recording is most of the code.

WHY IT LEFT `executor.py` AT ALL, given `executor_tools.py`'s header
argues at length that it could not. That argument was correct about the
harm and wrong about the remedy. Two guards hold this code down:
`test_errand_shell.test_env_install_is_a_job_and_not_a_foreground_call`
read `kernel/harness/executor.py` by path for `jobs_client.run_job(`,
and `test_a_version_specifier_is_not_a_redirect` took
`inspect.getsource(BridgeToolExecutor._env_install)` looking for
`shlex.quote`. Leaving the code behind to keep those guards pointed at a
real file was the lesser harm while `executor.py` was 280 lines; at 359,
against a 200 ceiling and with D3 about to walk 109 substrate branches
through it, it stopped being. So the code moved and BOTH GUARDS MOVED
WITH IT — the first now reads every `kernel/harness/executor*.py`, the
second reads this module's source. A guard that follows the code is
strictly better than one pinned to a path; a guard left behind is the
failure mode the original argument was protecting against, and it is
still a failure mode. Do not "simplify" either guard back to one path.

Takes the executor as `ex` for the same reason every function in
`executor_tools.py` does: it needs the bridge address AND the execution
id AND the connection, which is the executor spelled out longhand.
"""

from __future__ import annotations

import hashlib
import shlex

from kernel.bridge import jobs_client
from kernel.harness.models.types import ToolResult

MANAGERS = ("pip", "apt", "npm")

# THE ONE THIS TOOL WILL NOT RUN, and the reason has to be true on both
# substrates.
#
# ON KUBERNETES IT NEVER WORKED. `_security_context` sets
# `runAsNonRoot: True` on both containers and gives the sandbox its own
# uid, because the sandbox must not be able to overwrite the worker. So
# `apt-get install` ended in a permission error -- an opaque one, read
# as a broken package index or a network fault, retried with different
# flags.
#
# ON E0 IT DID WORK, and refusing it there is a real change. `spec.user`
# defaults to "" and nothing sets it, so the Docker substrate runs the
# sandbox as the image's own user -- root, for `python:3.12-slim`. That
# is precisely the divergence `env_guard` exists to close: a story
# developed locally with `apt-get install` is a story that fails in the
# cloud, and finding that out at dispatch is worse than finding it here.
#
# SO THE REFUSAL DOES NOT ARGUE FROM THE UID. "You are not root" is
# false on E0, and a refusal that states a falsehood on one substrate
# invites a model to work around it on that substrate -- the same defect
# as naming the Verifier while the switch is off. It argues from what
# SURVIVES instead, which is true everywhere: the container is
# discarded, the tree is rebuilt between rounds, and nothing apt did is
# in either. Only the base image carries a system package forward.
#
# `apt` STAYS IN `MANAGERS`. Dropping it would make this
# indistinguishable from a typo -- "not a manager this records" -- and
# the whole point is to say WHY and WHERE INSTEAD.
APT_REFUSAL = (
    "env_install: apt is refused. A system package installed here does "
    "not survive: the container is discarded and the tree is rebuilt "
    "from its committed manifests between rounds, so by your next leg "
    "it is gone -- and on Kubernetes it cannot even be installed, "
    "because the sandbox runs as a non-root user by design. A SYSTEM "
    "package belongs in the goal's BASE IMAGE: ask_human to have it "
    "added, or name an image that already carries it. If what you need "
    "is a PYTHON or NODE package, use env_install with pip or npm -- "
    "those install into the tree and are recorded."
)


def install_command(manager: str, packages: list) -> str:
    """The shell line for one install, with every package QUOTED.

    MEASURED ON EXECUTION 197. This joined the names raw, so
    `fastapi>=0.110.0` reached `sh` as the word `fastapi`, the operator
    `>`, and the word `=0.110.0` -- a REDIRECT. Five files named
    `=0.110.0`, `=0.27.0`, `=0.28.0`, `=2.0.0` and `=8.0.0` were left in
    the workspace root, one of them holding the pip output that should
    have gone back to the model.

    Three separate harms, none of them loud. The version constraint is
    silently dropped, so `>=0.110.0` installs whatever pip resolves. The
    output is redirected away, so the model is told nothing about what
    happened. And `record()` stores the packages AS WRITTEN, so the
    Verifier replays the same mangled command -- consistently wrong
    rather than divergent, which is the only mercy in it.

    `shlex.quote` and not a rejection of `>`: a specifier is a
    legitimate thing to ask for, and `env_install` is the tool the model
    is told to prefer over shelling out precisely so that what it
    installs is recorded.
    """
    from kernel.verifier.deps_layout import npm_install

    safe = " ".join(shlex.quote(p) for p in packages)
    # NOT `npm install -g`, and this is the whole reason npm was
    # unreplayable. A global install writes into the image's own tree;
    # the Verifier starts from the contract's digest and has no way to
    # carry that, so every npm delta was recorded `reproducible = false`
    # and the model was told its checks would run without it. A Node
    # story that installed a test runner ad-hoc and then wrote a check
    # needing it could never pass.
    #
    # `--prefix "$PWD"` is the workspace root -- the bridge runs every
    # bash with `cwd=WORKSPACE` (kernel/bridge/tools_core.py). node finds
    # `node_modules` by walking UP from the importing file, so a tree
    # there is on the path from every repo under `repos/<owner>__<name>`,
    # for `require` and for `import` alike. It is also outside every
    # repo, so nothing here is committed.
    #
    # The Verifier installs the SAME packages with the SAME flags into
    # its own prefix -- one declaration, in
    # kernel/verifier/deps_layout.py -- which is what makes the two
    # environments the same environment.
    return {
        "pip": f"python3 -m pip install --no-input {safe}",
        "apt": f"apt-get update && apt-get install -y {safe}",
        "npm": npm_install('"$PWD"', packages),
    }[manager]


def replay_note(manager: str, replayable) -> str:
    """What a replay will NOT carry, said to the model at install time.

    Honest about the limit rather than silent about it: pip and npm both
    go into a directory the Verifier hands to a container; apt mutates
    the image and cannot be carried from a clean base. A model that is
    not told this writes a check that needs the apt package and reads
    the failure as its own bug.
    """
    # npm FIRST, and the order is load-bearing: the original wrote the
    # not-replayable note and then overwrote it unconditionally for npm,
    # so npm's own note wins even if npm ever leaves REPLAYABLE. Saying
    # where the tree is matters more than saying it may not survive.
    if manager == "npm":
        # WHERE IT WENT, because it is not where `-g` used to put it and
        # a bare `jest` would now resolve nowhere. The Verifier puts the
        # same tree on PATH for a check; here the model has to spell the
        # path, the same way it already does for `.venv/bin/pytest`.
        return ("\n\nThese live in `node_modules/` at the WORKSPACE "
                "root, not globally. Imports resolve from any repo "
                "beneath it; run executables by path, e.g. "
                "`node_modules/.bin/jest`, or from inside a repo "
                "`../../node_modules/.bin/jest`. A check you write "
                "must do the same.")
    if manager not in replayable:
        return (f"\n\nNOTE: a {manager} install cannot be reproduced by "
                f"the Verifier from a clean base. It is recorded, and the "
                f"checks will run WITHOUT it. If a check needs it, say so "
                f"with ask_human rather than relying on it.")
    return ""


async def env_install(ex, conn, tool_args: dict,
                      tool_call_id: str) -> ToolResult:
    """Install into the sandbox, and record it so it can be replayed.

    The record is the point. This needs BOTH halves of the running
    execution — the connection to record through and the bridge address
    to install through — which is why it takes `ex`.
    """
    from kernel.verifier.environment import REPLAYABLE, record

    manager = (tool_args.get("manager") or "").strip()
    packages = [str(p) for p in (tool_args.get("packages") or [])
                if str(p).strip()]
    if manager not in MANAGERS:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"env_install: {manager!r} is not a manager this "
                  "records; use pip, apt or npm",
            exit_code=1)
    if manager == "apt":
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error=APT_REFUSAL, exit_code=1)
    if not packages:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="env_install: name at least one package",
                          exit_code=1)

    # E14 — A JOB, NOT A FOREGROUND CALL. `apt-get update && apt-get
    # install` routinely exceeds 30 seconds and this once took the flat
    # default, so a slow install came back as `bridge unreachable` — a
    # dead bridge, not a long download. Raising it to the model's 120s
    # ceiling only moved the cliff: a single large wheel (torch, a CUDA
    # build) goes past two minutes and came back as a failed install.
    # The budget is now enforced in the pod and nothing here waits on
    # the whole of it.
    #
    # KEYED ON WHAT IS BEING INSTALLED, so a second call for the same
    # packages joins the running install rather than starting a
    # concurrent pip against the same tree — which is what happened on
    # execution 200 when the first caller stopped listening.
    key = hashlib.sha1(  # noqa: S324 - a cache key, not a signature
        " ".join(packages).encode()).hexdigest()[:10]
    run = await jobs_client.run_job(
        install_command(manager, packages), host=ex._host, port=ex._port,
        key=f"env-install-{manager}-{key}")
    if run["state"] == "running":
        return ToolResult(
            tool_call_id=tool_call_id, error="", exit_code=0,
            output=f"still installing ({run['job']}, "
                   f"{run.get('elapsed_s') or 0:.0f}s so far). Call "
                   f"env_install again with the same packages to "
                   f"check on it — it re-attaches, it does not "
                   f"restart.\n\n" + (run["output"] or "")[-2000:])
    if run["state"] == "error" or run.get("exit_code") != 0:
        # NOT recorded. The Verifier reproduces what the executor HAS,
        # and a failed install left it with nothing.
        return ToolResult(
            tool_call_id=tool_call_id, output=run["output"],
            error=run.get("error")
                  or (f"{manager} stopped at its budget after "
                      f"{run.get('elapsed_s') or 0:.0f}s — a timeout, "
                      f"not a rejected package"
                      if run.get("ended") == "budget"
                      else f"{manager} install failed "
                           f"(exit {run.get('exit_code')})"),
            exit_code=1)

    await record(conn, ex._execution_id, manager, packages,
                 reproducible=manager in REPLAYABLE,
                 detail={"output": (run["output"] or "")[-1000:]})
    return ToolResult(
        tool_call_id=tool_call_id,
        output=f"installed via {manager}: {', '.join(packages)}\n"
               f"recorded, and reproduced when your work is "
               f"verified.{replay_note(manager, REPLAYABLE)}",
        error=None, exit_code=0)
