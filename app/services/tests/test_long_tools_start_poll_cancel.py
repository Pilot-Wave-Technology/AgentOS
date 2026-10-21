"""E14 · the harness gets start / poll / cancel, and the pod holds the job.

WHAT WAS WRONG. The model has had `bash_background` → `bash_output` →
`bash_kill` for a long time; the HARNESS had one blocking call with a
hard ceiling. `SETUP_MAX_S = 600` was a CLIFF, not a bound: an install
past it came back as `TimeoutError`, which reads as a broken manifest or
a dead network and is neither — KER25 parked on exactly that message at
the previous cliff of 120s. The call also held a tool-pool thread and
the shim's tool lock for the whole install, and a worker that restarted
mid-install lost it AND left it running.

WHAT THESE TESTS PIN, one property per measured failure:

  * the start call returns immediately and the BUDGET IS ENFORCED IN THE
    POD — no bridge call is ever long, which is the fact D5 needs;
  * a running job reports `exit_code: null`, never 0 and never -1
    (`k8s_outcome`: a placeholder exit code is read as a result, and a
    fabricated 1 becomes "the work is wrong");
  * the job OUTLIVES ITS POLLER, and starting again with the same key
    re-attaches instead of running a second `pip` against one tree —
    which execution 200 actually did;
  * `since` reads only what is new;
  * output that is not valid UTF-8 survives the round trip, because it
    travels base64 at a byte offset and never as a rendered string.
"""
from __future__ import annotations

import base64
import shutil
import time

import pytest

# The SANDBOX's own import path. `shim` puts `kernel/bridge/` on
# sys.path, exactly as the pod does, so `harness_jobs` here is the same
# module object the shim dispatches into — not a second copy under a
# package name, with a second job registry.
from kernel.bridge import shim                              # noqa: E402
import harness_jobs                                         # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="the shim runs bash")


def _call(tool: str, args: dict) -> dict:
    body = shim.execute_job_tool(tool, args)
    assert body["error"] is None, body["error"]
    return body["result"]


def _start(command: str, **args) -> str:
    return _call("job_start", {"command": command, **args})["job"]


def _until(job: str, predicate, limit: float = 20.0) -> dict:
    """Poll until the predicate holds. Returns the last answer."""
    end = time.time() + limit
    answer = _call("job_poll", {"job": job})
    while not predicate(answer) and time.time() < end:
        time.sleep(0.1)
        answer = _call("job_poll", {"job": job})
    return answer


def _exited(answer: dict) -> bool:
    return answer["state"] == "exited"


def test_a_running_job_has_no_exit_code():
    job = _start("sleep 3")
    answer = _call("job_poll", {"job": job})
    assert answer["state"] == "running"
    assert answer["exit_code"] is None, (
        "a placeholder exit code on a running job is read as a result -- "
        "see kernel/drivers/k8s_outcome.py, which paid for that once")
    assert answer["elapsed_s"] >= 0
    _call("job_cancel", {"job": job})


def test_the_start_call_is_short_and_the_pod_holds_the_budget():
    """THE WHOLE POINT. A thirty-minute install is a thirty-minute
    install, and no bridge call is ever long."""
    began = time.time()
    job = _start("sleep 30", budget_s=1)
    assert time.time() - began < 2.0, "start waited for the work"

    answer = _until(job, _exited, limit=15)
    assert answer["state"] == "exited"
    assert answer["ended"] == "budget", (
        "a signal death cannot say whether it was a budget, a cancel or "
        "an OOM kill, and 'your install failed' is the wrong sentence for "
        "all three")
    assert answer["exit_code"] is not None


def test_a_job_outlives_its_poller_and_a_restart_re_attaches():
    """A worker that restarts mid-install must find the install, not
    start a second one. Execution 200 started a second `pip` against one
    tree because the first caller stopped listening."""
    job = _start("echo begin; sleep 2; echo done", key="setup-77")

    # The poller goes away entirely -- no polls at all for a while.
    time.sleep(0.6)
    again = _call("job_start", {"command": "echo begin; sleep 2; echo done",
                                "key": "setup-77"})
    assert again["job"] == job and again["reattached"] is True

    answer = _until(job, _exited)
    text = base64.b64decode(answer["bytes"]).decode()
    assert text.count("begin") == 1, f"the command ran twice: {text!r}"
    assert answer["exit_code"] == 0 and answer["ended"] == "exit"


def test_a_finished_key_starts_a_fresh_run():
    """The other half of re-attaching: once it is done, calling again is
    a re-run, or a caller would read the previous run's exit code as an
    answer about this one."""
    first = _start("echo once", key="finished-1")
    _until(first, _exited)
    second = _call("job_start", {"command": "exit 3", "key": "finished-1"})
    assert second["reattached"] is False
    answer = _until(second["job"], _exited)
    assert answer["exit_code"] == 3


def test_since_reads_only_what_is_new():
    job = _start("echo one; sleep 1; echo two")
    first = _until(job, lambda a: b"one" in base64.b64decode(a["bytes"]))
    cursor = first["offset"]
    assert cursor > 0

    _until(job, _exited)
    answer = _call("job_poll", {"job": job, "since": cursor})
    later = base64.b64decode(answer["bytes"])
    assert b"two" in later and b"one" not in later
    assert answer["offset"] >= cursor
    assert answer["dropped"] == 0


def test_output_that_is_not_utf8_survives_the_round_trip():
    r"""`runner.py`'s argument, applied here: a result stays one line and
    stays parseable whatever the command emitted. `\xff\xfe` is not
    valid UTF-8 and a rendered channel would have mangled it."""
    job = _start(r"printf 'a\xff\xfeb'")
    answer = _until(job, _exited)
    assert base64.b64decode(answer["bytes"]) == b"a\xff\xfeb"


def test_cancel_stops_it_and_is_idempotent():
    job = _start("sleep 30")
    assert _call("job_cancel", {"job": job}) == {"state": "cancelled",
                                                 "was_running": True}
    answer = _until(job, _exited)
    assert answer["ended"] == "cancel"
    # Cancelling a finished job is not an error: a caller that gave up
    # and a job that finished are a race, not a mistake.
    assert _call("job_cancel", {"job": job})["was_running"] is False


def test_finished_jobs_do_not_accumulate_for_ever():
    """Each one holds its output buffer, and a verification pod runs
    many check jobs through one shim."""
    import jobcore
    for n in range(jobcore.KEEP_FINISHED + 3):
        _until(_start("true", key=f"prune-{n}"), _exited)
    # The registry is `jobcore`'s since the merge; the pruning rule
    # is the same one and is now shared with the model's lane.
    assert len(jobcore.known(harness_jobs.LANE)) <= jobcore.KEEP_FINISHED + 1


def test_an_unknown_job_says_what_it_knows():
    body = shim.execute_job_tool("job_poll", {"job": "hjob-nope"})
    assert body["error_code"] == "E_BAD_ARGUMENT"
    assert "hjob-nope" in body["error"]


def test_a_budget_out_of_range_is_refused_before_anything_runs():
    body = shim.execute_job_tool("job_start", {"command": "true",
                                               "budget_s": 99999})
    assert body["error_code"] == "E_BAD_ARGUMENT"
    assert "7200" in body["error"]


def test_a_cwd_that_is_not_there_is_refused_rather_than_swapped():
    """An install that quietly ran in another directory reports success
    and changes nothing anybody can find."""
    body = shim.execute_job_tool("job_start", {"command": "true",
                                               "cwd": "/no/such/place"})
    assert body["error_code"] == "E_PATH_NOT_FOUND"


def test_the_whole_round_trip_over_http(tmp_path):
    """THE SEAM BOTH HALVES HAVE TO AGREE ON. Everything above runs the
    shim's handlers in-process; this one runs a real shim in a real
    process and drives it with the real client, which is the only way to
    catch the payload being nested under a key the client does not read.
    """
    import asyncio
    import json
    import os
    import subprocess
    import sys
    import urllib.request

    port = 9397
    env = dict(os.environ, WORKSPACE=str(tmp_path), BRIDGE_PORT=str(port))
    proc = subprocess.Popen([sys.executable, shim.__file__,
                             "--bind", "127.0.0.1"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        end = time.time() + 20
        while time.time() < end:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2).read()
                break
            except Exception:                               # noqa: BLE001
                time.sleep(0.3)
        else:
            raise AssertionError("shim never became healthy")

        from kernel.bridge import jobs_client

        out = asyncio.run(jobs_client.run_job(
            "echo hello; exit 4", host="127.0.0.1", port=port,
            budget_s=30, key="e2e-1", wait_s=20))
        assert out["state"] == "exited" and out["exit_code"] == 4
        assert out["output"].strip() == "hello"
        assert out["ended"] == "exit"
        assert json.loads(json.dumps(out)) == out, "not JSON-serialisable"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_the_job_ops_never_take_the_tool_lock():
    """A poll queued behind the model's 120s foreground `bash` is the
    wait this change exists to remove."""
    import jobcore
    assert jobcore._lock is not shim._TOOL_LOCK
    acquired = shim._TOOL_LOCK.acquire(timeout=1)
    try:
        began = time.time()
        job = _start("sleep 5")
        answer = _call("job_poll", {"job": job})
        assert time.time() - began < 2.0
        assert answer["state"] == "running"
        _call("job_cancel", {"job": job})
    finally:
        if acquired:
            shim._TOOL_LOCK.release()
