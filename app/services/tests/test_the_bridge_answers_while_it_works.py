"""A long tool call must not read as a dead sandbox.

WHY A STORY KEPT LOSING ITS WORKSPACE AND AN ERRAND NEVER DID.

The shim was `HTTPServer`: single-threaded, one request served to
completion before the next is read. `env_setup` runs the tree's own
install -- `pip install -r requirements.txt`, `npm ci` -- and did it in
one blocking call, and for that whole time the bridge answered nothing
at all. (E14 made that caller a job, so the install no longer holds
anything; the model's own 120s foreground `bash` still can, which is
why this property still matters.)

`worker/loop.py` probes `bridge_health(timeout=3)`. The conductor runs
two replicas, so the worker NOT running the leg is the one probing:
three seconds into the install it concluded the sandbox was dead.

    execution 192: bridge dead; destroying the pod so dispatch
    rebuilds it (the claim, and the tree on it, survive)

From outside that is "the workspace disappears the moment it reaches
env_setup", repeatedly -- the file explorer reads the live pod, and
there was no live pod. Measured on NCAL4 runs 191 and 192, both at the
first step after plan approval.

AND IT IS WHY ERRAND 187 WORKED. An errand writes its report at the
workspace root and never calls `env_setup`: nothing held the bridge for
more than a moment, every probe answered, the pod lived, and it finished
cleanly. A coding story is the first shape of work that blocks the
bridge for minutes. Same code, different work.

Measured both ways with one 8-second call:

    HTTPServer          health TIMED OUT after 3.03s
    ThreadingHTTPServer health 200 in 0.02s, tool still finished 0

THE LOCK IS NOT AN AFTERTHOUGHT. Threading this server is about
LIVENESS being answerable during a long call. It is not an offer of
concurrent tool execution against one workspace, which nothing in this
design has ever made -- handing that over by accident while fixing a
health probe would be the worse bug.
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from kernel.bridge import shim

_PORT = 9397


def test_the_server_is_threaded():
    """THE FIX, at its one line."""
    src = inspect.getsource(shim)
    assert "ThreadingHTTPServer(" in src, (
        "a single-threaded bridge cannot answer /health during an "
        "env_setup, and the health probe's timeout is 3 seconds")
    # `= HTTPServer(` and not a bare substring: `ThreadingHTTPServer(`
    # contains `HTTPServer(`, so the naive check passes either way.
    assert "server = HTTPServer(" not in src


def test_tools_are_still_serialised():
    src = inspect.getsource(shim.BridgeHandler.do_POST)
    assert "_TOOL_LOCK" in src, (
        "threading the server must not hand over concurrent tool "
        "execution against one workspace")


def test_health_is_not_behind_the_lock():
    """If it were, the fix would be no fix: the probe would queue behind
    the install exactly as before."""
    assert "_TOOL_LOCK" not in inspect.getsource(shim.BridgeHandler.do_GET)


def test_threads_do_not_outlive_the_pod():
    assert "daemon_threads = True" in inspect.getsource(shim.main)


# ── and the behaviour, because the above is just spelling ────────────

@pytest.mark.skipif(os.name == "nt" and not os.environ.get("AGENTOS_RUN_SHIM"),
                    reason="spawns a server and a sleeping shell")
def test_health_answers_while_a_tool_is_running(tmp_path):
    env = dict(os.environ, WORKSPACE=str(tmp_path), BRIDGE_PORT=str(_PORT))
    proc = subprocess.Popen(
        [sys.executable, shim.__file__, "--bind", "127.0.0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_for_health(proc)
        done = []

        def _slow():
            body = json.dumps({"tool": "bash",
                               "args": {"command": "sleep 5", "timeout": 30},
                               "id": "t1"}).encode()
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{_PORT}/tool", data=body,
                    headers={"Content-Type": "application/json"}),
                    timeout=40).read()
                done.append(True)
            except Exception:                               # noqa: BLE001
                done.append(False)

        worker = threading.Thread(target=_slow)
        worker.start()
        time.sleep(1.0)                 # the tool is now in flight

        # THE PROBE THE CONDUCTOR MAKES, with its own timeout.
        started = time.time()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{_PORT}/health", timeout=3) as resp:
            assert resp.status == 200
        assert time.time() - started < 3, (
            "health queued behind the tool call, which is the pod being "
            "destroyed under a healthy run")

        worker.join(timeout=40)
        assert done == [True], "the tool call itself must still complete"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _wait_for_health(proc, deadline: float = 20.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        if proc.poll() is not None:
            raise AssertionError(f"shim exited: {proc.stdout.read()[:400]}")
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{_PORT}/health", timeout=2).read()
            return
        except Exception:                                   # noqa: BLE001
            time.sleep(0.3)
    raise AssertionError("shim never became healthy")
