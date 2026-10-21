"""E14 · the ceilings after, and the caller half of start/poll/cancel.

THE TABLE THIS PINS (plan §12.3):

    model foreground `bash`        120s, UNCHANGED — the nudge works
    harness foreground             30s  — none of these are long
    harness job budget             1800s default, 7200s ceiling
    the poll call itself           10s  — it returns what exists

`SETUP_MAX_S` and `SETUP_FLAG` retire with the blocking path, and the
last two tests are the reconciliation: a caller that still reaches for
the flag, or still sends a literal the sandbox would refuse, fails here
rather than in a story.
"""
from __future__ import annotations

import asyncio
import base64
import io
import pathlib

import pytest

from kernel.bridge import jobs_client, limits

ROOT = pathlib.Path(__file__).resolve().parents[3]


# ── the numbers ──────────────────────────────────────────────────────

def test_the_model_facing_ceiling_did_not_move():
    """Raising it is the tempting fix and the wrong one: a foreground
    command holds the leg, and `bash_background` is the answer."""
    assert limits.BASH_MAX_S == 120
    with pytest.raises(limits.BadTimeout) as caught:
        limits.bash_timeout({"timeout": 600})
    assert "bash_background" in str(caught.value)


def test_the_harness_escape_hatch_is_gone():
    """It was a nudge towards the right tool, and the right tool exists
    now. A flag that still worked would keep a 600s blocking call
    available to anybody who remembered the name."""
    assert not hasattr(limits, "SETUP_FLAG")
    assert not hasattr(limits, "SETUP_MAX_S")
    # And it is not honoured by accident under its old name.
    with pytest.raises(limits.BadTimeout):
        limits.bash_timeout({"timeout": 600, "harness_setup": True})


def test_the_harness_foreground_is_short_now():
    assert limits.HARNESS_FOREGROUND_S == 30
    assert limits.bash_timeout(
        {"timeout": limits.HARNESS_FOREGROUND_S}) == 30


def test_the_job_ceiling_is_the_verifier_s_own_budget():
    """A check against `budget.MAX_S` through a 600s channel was twelve
    times over. Restated here because the sandbox cannot import the
    verifier; this is what stops the two drifting."""
    from kernel.verifier.budget import MAX_S

    assert limits.JOB_MAX_S == int(MAX_S)
    assert limits.JOB_DEFAULT_S == 1800
    assert limits.job_budget({}) == limits.JOB_DEFAULT_S
    assert limits.job_budget({"budget_s": 3600}) == 3600


@pytest.mark.parametrize("bad", [0, -1, "600", 1.5, True, 7201])
def test_a_budget_is_refused_rather_than_clamped(bad):
    """A caller that asked for three hours and silently got two reads
    the kill as its command failing."""
    with pytest.raises(limits.BadTimeout):
        limits.job_budget({"budget_s": bad})


def test_the_poll_call_is_never_long():
    assert limits.POLL_MAX_S == 10
    assert limits.bridge_timeout(limits.JOB_POLL, {}) == limits.POLL_MAX_S
    for tool in (limits.JOB_START, limits.JOB_CANCEL):
        assert limits.bridge_timeout(tool, {}) == limits.BRIDGE_DEFAULT_S
    # And the worker still out-waits the sandbox on a foreground bash.
    assert limits.bridge_timeout("bash", {"timeout": 120}) > 120


def test_the_models_tools_did_not_change():
    """§12.6: `bash`, `bash_background`, `bash_output` and `bash_kill`
    keep their schemas, their ceilings and their prose. The job ops are
    harness-only, and what keeps a model out is that no profile can name
    them."""
    from kernel.tools.catalog import tools_by_side

    sandbox = set(tools_by_side("sandbox"))
    assert {"bash", "bash_background", "bash_output", "bash_kill"} <= sandbox
    assert not [t for t in sandbox if t.startswith("job_")]


# ── the caller half, without a cluster ───────────────────────────────

def _fake_bridge(monkeypatch, answers):
    """Replace the one synchronous HTTP call. Everything above it — the
    cursor, the accumulation, the give-up clock — is exercised for real."""
    sent = []

    def _post(tool, args, host, port, timeout):
        sent.append((tool, dict(args)))
        if tool == limits.JOB_START:
            return {"output": "", "error": None,
                    "result": {"job": "hjob-test", "reattached": False}}
        if tool == limits.JOB_CANCEL:
            return {"output": "", "error": None,
                    "result": {"state": "cancelled", "was_running": True}}
        return {"output": "", "error": None,
                "result": answers.pop(0) if answers else _RUNNING}

    monkeypatch.setattr(jobs_client, "_post", _post)
    return sent


_RUNNING = {"state": "running", "exit_code": None, "bytes": "", "offset": 0,
            "elapsed_s": 1.0, "ended": None, "dropped": 0}


def _poll(state, chunk: bytes, offset: int, **extra):
    answer = dict(_RUNNING, state=state, offset=offset,
                  bytes=base64.b64encode(chunk).decode())
    answer.update(extra)
    return answer


def test_run_job_follows_the_cursor_and_joins_the_pieces(monkeypatch):
    """A multi-byte character split across two polls decodes only if the
    halves are joined as BYTES first."""
    monkeypatch.setattr(jobs_client, "FIRST_INTERVAL_S", 0.01)
    sent = _fake_bridge(monkeypatch, [
        _poll("running", "caf".encode() + b"\xc3", 4),
        _poll("exited", b"\xa9 ok", 9, exit_code=0, ended="exit"),
        _poll("exited", b"", 9, exit_code=0, ended="exit"),
    ])
    out = asyncio.run(jobs_client.run_job("build", wait_s=30))

    assert out["state"] == "exited" and out["exit_code"] == 0
    assert out["output"] == "café ok"
    polls = [args for tool, args in sent if tool == limits.JOB_POLL]
    assert [p["since"] for p in polls] == [0, 4, 9], (
        "the cursor did not advance, so a poll re-sent bytes it already had")


def test_a_pass_that_runs_out_of_patience_reports_running(monkeypatch):
    """NOT a failure and never an exit code: the work is still going,
    and the next pass re-attaches by key."""
    monkeypatch.setattr(jobs_client, "FIRST_INTERVAL_S", 0.01)
    _fake_bridge(monkeypatch, [])
    out = asyncio.run(jobs_client.run_job("pip install .", wait_s=0.05))

    assert out["state"] == "running"
    assert out["exit_code"] is None
    assert out["job"] == "hjob-test"


def test_a_bridge_fault_is_not_a_failed_install(monkeypatch):
    """The misdiagnosis this whole change exists to stop: the job is
    still in the pod, so a lost poll says nothing about the work."""
    polls = []

    def _post(tool, args, host, port, timeout):
        if tool == limits.JOB_START:
            return {"output": "", "error": None,
                    "result": {"job": "hjob-test", "reattached": False}}
        polls.append(args)
        return {"error": "bridge unreachable: timed out"}

    monkeypatch.setattr(jobs_client, "FIRST_INTERVAL_S", 0.01)
    monkeypatch.setattr(jobs_client, "_post", _post)
    out = asyncio.run(jobs_client.run_job("npm ci", wait_s=5))
    assert out["state"] == "error" and out["exit_code"] is None
    assert "unreachable" in out["error"]
    assert len(polls) == jobs_client.POLL_RETRIES, (
        "one dropped poll was believed; the job is still in the pod")


def test_the_key_travels_so_a_restart_re_attaches(monkeypatch):
    sent = _fake_bridge(monkeypatch, [
        _poll("exited", b"done", 4, exit_code=0, ended="exit"),
        _poll("exited", b"", 4, exit_code=0, ended="exit"),
    ])
    asyncio.run(jobs_client.run_job("x", key="env-setup-7-0", budget_s=60))
    start = [args for tool, args in sent if tool == limits.JOB_START][0]
    assert start["key"] == "env-setup-7-0" and start["budget_s"] == 60


# ── reconciliation ───────────────────────────────────────────────────

def _kernel_sources():
    for path in sorted(ROOT.glob("kernel/**/*.py")):
        if "__pycache__" in str(path):
            continue
        yield (str(path.relative_to(ROOT)).replace("\\", "/"),
               io.open(path, encoding="utf-8").read())


def test_nothing_still_reaches_for_the_retired_flag():
    # `limits.py` is where the history belongs and it names the retired
    # constants to explain what replaced them. Everywhere else, naming a
    # symbol that no longer exists sends the next reader to grep for
    # nothing.
    offenders = [rel for rel, src in _kernel_sources()
                 if not rel.endswith("bridge/limits.py")
                 and ("SETUP_FLAG" in src or "SETUP_MAX_S" in src)]
    assert not offenders, (
        f"{offenders} still name the retired harness escape hatch; the "
        f"long path is job_start/job_poll now")


def test_no_bash_call_asks_for_more_than_the_sandbox_allows():
    """The general form, unchanged in spirit from F3: a literal past the
    ceiling is refused with E_BAD_ARGUMENT and every command fails
    identically, whatever it was trying to do."""
    import re

    offenders = []
    for rel, src in _kernel_sources():
        if rel.endswith("limits.py"):
            continue
        for literal in re.findall(r'"timeout":\s*(\d+)', src):
            if int(literal) > limits.BASH_MAX_S:
                offenders.append(f"{rel}: timeout {literal}")
    assert not offenders
