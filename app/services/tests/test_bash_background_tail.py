"""A background job's buffer keeps the END, and says so while running.

TWO BUGS, one shape. `bash_background` exists so an executor can watch a
long build — the module's own docstring says "our own gate takes twelve
minutes". It then:

  kept the FIRST 32 KB and discarded everything after, so watching a build
  showed its startup banner and import warnings and never the failure; and

  set `truncated` only after `proc.wait()`, so a live job past the cap
  returned byte-identical output on every poll with no indication it was
  capped. That reads as a hung process, and the correct response to a hung
  process is `bash_kill` — the tool taught the model to kill healthy runs.

`tools_jobs` is sandbox-side and uses bare-name imports (`import errors`),
the way the shim loads it, so these add the bridge directory to the path
rather than importing through the `kernel.bridge` package.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import time

import pytest

_BRIDGE = str(pathlib.Path(__file__).resolve().parents[3] / "kernel" / "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

tools_jobs = pytest.importorskip("tools_jobs")

# A line long enough that a few hundred of them clear the cap quickly.
_LINE = "x" * 200


# NOT "." -- see the note in test_a_long_tool_behaves: the workspace is
# where a job spills its whole log, and "." is the repository.
_SCRATCH = tempfile.mkdtemp(prefix="agentos-tailtest-")


def _start(command: str) -> str:
    import os

    res = tools_jobs.tool_bash_background({"command": command}, _SCRATCH,
                                          dict(os.environ))
    found = re.search(r"job-[0-9a-f]+", str(res))
    assert found, f"no job id in {res}"
    return found.group(0)


def _poll(job_id: str) -> str:
    return tools_jobs.tool_bash_output({"job_id": job_id}).get("output", "")


def _wait(job_id: str, seconds: float = 12.0) -> str:
    deadline = time.time() + seconds
    body = ""
    while time.time() < deadline:
        body = _poll(job_id)
        if "finished" in body:
            return body
        time.sleep(0.2)
    return body


def test_the_buffer_keeps_the_tail_not_the_head():
    """THE REGRESSION. The last thing a job printed is the thing worth
    having; the first is a banner."""
    job = _start(
        'python -c "'
        "print('HEAD-MARKER');"
        f"[print('{_LINE}') for _ in range(400)];"
        "print('TAIL-MARKER')"
        '"')
    body = _wait(job)
    assert "finished" in body, "job did not finish in time"
    assert "TAIL-MARKER" in body, "the end of the output was discarded"
    assert "HEAD-MARKER" not in body, (
        "the head survived a job well over the cap — the buffer is still "
        "keeping the wrong end")


def test_it_says_which_end_was_dropped():
    """A model told only "truncated" reads the last line as the last thing
    that happened, rather than as the newest of a longer run."""
    job = _start(f"python -c \"[print('{_LINE}') for _ in range(400)]\"")
    body = _wait(job)
    assert "earlier output dropped" in body
    assert "truncated at 32 KB" not in body, "the old, misleading wording"


def test_a_live_capped_job_admits_it_is_capped():
    """The second bug, and the one that made a healthy job look hung.

    Polled WHILE RUNNING, past the cap, the notice must already be there.
    """
    job = _start(
        'python -c "'
        "import time,sys;"
        f"[(print('{_LINE}'), sys.stdout.flush()) for _ in range(400)];"
        "time.sleep(4)"
        '"')
    deadline = time.time() + 8
    saw = ""
    while time.time() < deadline:
        saw = _poll(job)
        if "running" in saw and "earlier output dropped" in saw:
            break
        time.sleep(0.2)
    tools_jobs.tool_bash_kill({"job_id": job})
    assert "earlier output dropped" in saw, (
        "a running job past the cap gave no sign it was capped — it reads "
        "as hung, and the model kills it")


def test_a_small_job_is_untouched():
    """Story executors run short commands constantly. Nothing may change
    for output that fits."""
    job = _start("python -c \"print('small and complete')\"")
    body = _wait(job)
    assert "small and complete" in body
    assert "earlier output dropped" not in body


def test_the_buffer_never_exceeds_the_cap():
    """The trim runs on every line, so the buffer cannot grow past the
    cap between polls — which is what would refill the pipe."""
    job = _start(f"python -c \"[print('{_LINE}') for _ in range(600)]\"")
    body = _wait(job)
    # Plus the status line and the dropped-output notice.
    assert len(body) < tools_jobs.MAX_OUTPUT + 512, len(body)
