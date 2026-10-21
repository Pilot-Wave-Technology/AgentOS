"""Every `call_bridge` says how long it will wait.

THE DEFECT, AND IT IS THE SECOND TIME THIS EXACT CLASS HAS BITTEN.
`kernel/bridge/limits.py` opens by naming it:

    "AND THE HALF THAT WOULD HAVE MADE THE FIX A LIE. `call_bridge` uses
    a 30s HTTP read timeout of its own. Putting `timeout` in the schema
    without raising that would turn a 90-second command into
    `bridge unreachable: timed out` at 30 seconds -- the same failure,
    now with the model's own argument apparently ignored."

`bridge_timeout()` exists to compute the right one. Three call sites
never used it, and one of them was `run_setups`:

    call_bridge("bash", {"command": ..., "timeout": SETUP_MAX_S,
                         SETUP_FLAG: True},
                f"env-setup-{execution_id}", port=port, host=host)

The args tell the SANDBOX it may take ten minutes, because
`pip install -r requirements.txt` on a cold container legitimately can.
The WORKER gave up after thirty seconds.

WHY THAT IS WORSE THAN A SLOW CALL. The sandbox does not stop. It keeps
installing and keeps holding `_TOOL_LOCK`, so the lock outlives the
caller -- and every later tool call queues behind a job nobody is
waiting for any more. Measured on execution 200:

    20:26:39  env_setup            pip starts, takes the lock
    20:27:10  env_setup    (+31s)  the model retried -- a SECOND pip
    20:27:41  bash ls -la  (+31s)  timed out
    20:28:27  bash ls -la  (+46s)  timed out
    20:29:43  bash_background      gave up on foreground bash
    20:30:17  write                worked -- worker-side, never the bridge

Every 31-second gap is this default. And the install itself takes TEN
SECONDS cold, measured in the same sandbox: 1855 files, no wheel cache.
Nothing here was slow. One caller stopped listening.

E14 REPLACED THE CALL RATHER THAN THE NUMBER. `run_setups` starts a job
and polls it, so there is no long HTTP read to get right and a caller
that stops listening leaves an install running that the next tick
re-attaches to. The guard below still applies to every `call_bridge`
that remains.

IT WORKED ON DOCKER because a warm pip cache in a reused volume
finished inside thirty seconds, so the timeout never fired. A fresh PVC
per execution is what changed, and it changed the timing rather than the
code.

AN AST WALK, NOT A GREP: a grep cannot tell a call from the prose above,
and this file discusses `call_bridge` at length.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _sites():
    for path in sorted(ROOT.glob("kernel/**/*.py")):
        if "test" in path.parts or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id",
                           getattr(node.func, "attr", None))
            if name == "call_bridge":
                yield path.relative_to(ROOT), node


def test_every_call_bridge_passes_a_timeout():
    """The guard. A site without one silently takes BRIDGE_TIMEOUT."""
    bare = [f"{p}:{n.lineno}" for p, n in _sites()
            if "timeout" not in {k.arg for k in n.keywords}]
    assert not bare, (
        "these call_bridge sites take the 30s default, so a longer "
        "command is abandoned mid-run while the sandbox keeps holding "
        f"the tool lock: {bare}. Pass timeout=bridge_timeout(tool, args).")


def test_there_are_sites_to_check():
    """A guard that finds nothing to guard is a guard that has stopped
    working -- this suite has been bitten by that shape too."""
    assert len(list(_sites())) >= 3


def test_the_worker_always_waits_longer_than_the_sandbox():
    """The invariant `bridge_timeout` exists to hold: if the sandbox may
    spend N seconds, the worker must wait more than N, or a command that
    times out reports `bridge unreachable` instead of its own E_TIMEOUT
    -- which reads as a dead pod and gets one destroyed."""
    from kernel.bridge.limits import BASH_DEFAULT_S, bridge_timeout

    for args in ({}, {"timeout": 10}, {"timeout": BASH_DEFAULT_S},
                 {"timeout": 120}):
        allowed = args.get("timeout", BASH_DEFAULT_S)
        assert bridge_timeout("bash", args) > allowed, (
            f"worker waits {bridge_timeout('bash', args)}s for a command "
            f"the sandbox may spend {allowed}s on")


def test_the_setup_path_no_longer_has_a_long_call_to_out_wait():
    """E14 ANSWERED THIS DIFFERENTLY, and the difference is the point of
    the change. The old fix was "the worker must wait longer than the
    ten minutes the sandbox may spend"; now the sandbox spends the time
    on a JOB and every call about it is short, so there is no long wait
    to get right. A poll that is lost costs a tick, not an install."""
    from kernel.bridge.limits import (BRIDGE_DEFAULT_S, JOB_POLL,
                                      JOB_START, POLL_MAX_S, bridge_timeout)

    assert bridge_timeout(JOB_POLL, {}) == POLL_MAX_S <= 10
    assert bridge_timeout(JOB_START, {}) == BRIDGE_DEFAULT_S

    src = (ROOT / "kernel/worker/envsetup.py").read_text(encoding="utf-8")
    assert "jobs_client.run_job(" in src, "run_setups still blocks"
