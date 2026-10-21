"""A busy sandbox answers "busy". It does not let the caller time out.

THIS IS THE ORIGINAL COMPLAINT, ROOT-CAUSED.

    "tool bridge seems not to respond properly"
    "the tool bridge is unresponsive with TimeoutError on all operations"

The second line is how a model reported it, from inside a run, before
parking to ask a person what was wrong.

WHAT IS ACTUALLY HAPPENING. Tools run one at a time in a sandbox, which
is deliberate -- two bash calls against one workspace is not something
this design has ever offered. `env_setup` runs the tree's own
`pip install -r requirements.txt` and used to hold the lock for all of
it -- up to TEN MINUTES (E14 made that caller a job; a model's
foreground `bash` can still hold it for 120s). Every other tool call in that
window simply waited, and each one failed when ITS OWN http timeout
expired -- 25 seconds for a trivial call, 135 for an install.

Measured on exec-199, and the pair of facts is the worst possible one:

    /health          200 in 0.04s
    bash "echo hi"   TimeoutError after 25.0s

So the sandbox reports itself alive and refuses to do anything, and says
neither. `env_install` looked broken for an afternoon on the strength of
it; it was simply the next caller to arrive after `env_setup`.

WHY A REFUSAL BEATS A TIMEOUT even though both fail the call:

  it arrives in two seconds instead of at the caller's ceiling;
  it NAMES the holder and how long it has held -- "env_setup, 214s" points
  at a requirements file, "TimeoutError" points at the network;
  and it is distinguishable from a dead bridge, which matters because
  `ensure_reachable` destroys a pod it believes is dead. A pod that was
  only busy installing was being torn down and rebuilt.

THE CLIENT HALF IS NOT OPTIONAL. `urlopen` raises on any non-2xx and
`HTTPError` subclasses `URLError`, so without an arm for it the 409
arrives as "bridge unreachable" -- the exact misdiagnosis the 409 was
added to prevent, with the message thrown away for a status line.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from kernel.bridge import shim

_PORT = 9398

pytestmark = pytest.mark.skipif(
    os.name == "nt" and not os.environ.get("AGENTOS_RUN_SHIM"),
    reason="spawns a server and a sleeping shell")


def _post(tool: str, args: dict, call_id: str, timeout: float = 30):
    body = json.dumps({"tool": tool, "args": args, "id": call_id}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{_PORT}/tool", data=body,
        headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read()), time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), time.time() - started


def _wait_healthy(proc, deadline: float = 20.0) -> None:
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


@pytest.fixture()
def running_shim(tmp_path):
    env = dict(os.environ, WORKSPACE=str(tmp_path), BRIDGE_PORT=str(_PORT))
    proc = subprocess.Popen(
        [sys.executable, shim.__file__, "--bind", "127.0.0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_healthy(proc)
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ── the behaviour ────────────────────────────────────────────────────

def test_a_second_caller_is_refused_not_hung(running_shim):
    """THE DEFECT. The second call must come back quickly, with a reason
    -- not sit until its own timeout and report a dead bridge."""
    holder = threading.Thread(
        target=_post, args=("bash", {"command": "sleep 12", "timeout": 30},
                            "holder", 40))
    holder.start()
    time.sleep(1.0)                      # the lock is now held
    try:
        status, body, waited = _post(
            "bash", {"command": "echo hi", "timeout": 10}, "second", 30)

        assert status == 409, f"expected a refusal, got {status}"
        assert waited < 6, (
            f"the caller waited {waited:.1f}s -- it is queueing, which is "
            f"the silent hang this replaces")
        assert "E_SANDBOX_BUSY" in (body.get("error") or "")
        assert body.get("exit_code") == 1
    finally:
        holder.join(timeout=40)


def test_the_refusal_names_the_holder_and_its_age(running_shim):
    """"env_setup, 214s" points at a requirements file. "TimeoutError"
    points at the network, and it is the wrong place."""
    holder = threading.Thread(
        target=_post, args=("bash", {"command": "sleep 10", "timeout": 30},
                            "holder", 40))
    holder.start()
    time.sleep(3.0)
    try:
        _, body, _ = _post("bash", {"command": "echo hi"}, "second", 30)
        error = body.get("error") or ""
        assert "bash" in error, f"the holder is not named: {error}"
        assert "NOT a dead bridge" in error, (
            "the one thing the reader must not conclude")
        # The age, so a two-second hold and a ten-minute one read
        # differently.
        assert any(ch.isdigit() for ch in error)
    finally:
        holder.join(timeout=40)


def test_health_still_answers_while_busy(running_shim):
    """Unchanged, and load-bearing: `ensure_reachable` destroys a pod it
    believes is dead."""
    holder = threading.Thread(
        target=_post, args=("bash", {"command": "sleep 8", "timeout": 30},
                            "holder", 40))
    holder.start()
    time.sleep(1.0)
    try:
        started = time.time()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{_PORT}/health", timeout=3) as resp:
            assert resp.status == 200
        assert time.time() - started < 3
    finally:
        holder.join(timeout=40)


def test_the_lock_is_released_after_a_refusal(running_shim):
    """A refusal must not consume the lock it failed to take -- that
    would turn one slow tool into a permanently dead sandbox."""
    holder = threading.Thread(
        target=_post, args=("bash", {"command": "sleep 6", "timeout": 30},
                            "holder", 40))
    holder.start()
    time.sleep(0.5)
    _post("bash", {"command": "echo refused"}, "refused", 30)
    holder.join(timeout=40)

    status, body, _ = _post("bash", {"command": "echo after"}, "after", 30)
    assert status == 200, f"the sandbox never recovered: {body}"
    assert "after" in (body.get("output") or "")


def test_an_idle_sandbox_is_not_refused(running_shim):
    """The ordinary case. A two-second acquire timeout must not make a
    quiet sandbox look busy."""
    status, body, waited = _post("bash", {"command": "echo fine"}, "solo", 30)
    assert status == 200 and "fine" in (body.get("output") or "")
    assert waited < 5


# ── and the client reports it as a refusal, not as unreachable ───────

def test_the_client_surfaces_the_busy_message():
    """`HTTPError` subclasses `URLError`, so the arm order matters: catch
    it second and the message becomes "bridge unreachable", which sends
    the reader to the network for a pod that is answering fine."""
    import inspect

    from kernel.bridge import client

    src = inspect.getsource(client.call_bridge)
    assert "except HTTPError" in src
    assert src.index("except HTTPError") < src.index("except URLError"), (
        "URLError catches HTTPError first, so the body is never read")
    assert 'body.get("error")' in src
