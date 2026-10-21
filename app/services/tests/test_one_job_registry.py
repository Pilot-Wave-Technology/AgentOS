"""The model's jobs and the harness's are one mechanism, two presenters.

WHY THEY MERGED. `tools_jobs` served the model and `harness_jobs` served
the harness, with separate dicts, separate locks, separate ids -- and
only one of them was finished. The harness side had a budget watchdog, a
group kill, a reap grace for a grandchild holding the pipe, and an
`ended` reason distinguishing a timeout from a failure. The model side
had none of the four: `bash_background` took no budget and `bash_kill`
called `proc.kill()`, which signals the shell and leaves `pip`'s
children writing into a tree the next leg is editing.

The alternative was building the missing half a second time, and two
implementations of one mechanism drift. `harness_jobs.py`'s own docstring
argues for the separation -- correctly, about PRESENTATION. This file
pins the line between the two: what they say may differ, what they do
may not.

AND IT PINS THE NON-REGRESSION. A merge is only safe if a story that was
working keeps working, so the first tests here are about what did NOT
change: the model's tool names, its job-count ceiling, and the absence
of a budget on `bash_background`, which is a separate deliberate change.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_BRIDGE = str(pathlib.Path(__file__).resolve().parents[3] / "kernel" / "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

jobcore = pytest.importorskip("jobcore")
tools_jobs = pytest.importorskip("tools_jobs")
harness_jobs = pytest.importorskip("harness_jobs")


# ── one mechanism ───────────────────────────────────────────────────

def test_there_is_one_registry():
    """Neither presenter keeps its own."""
    for module in (tools_jobs, harness_jobs):
        assert not hasattr(module, "_jobs"), (
            f"{module.__name__} still has its own registry")
        assert not hasattr(module, "_lock")


def test_neither_presenter_spawns_a_process_itself():
    """A second `Popen` is a second set of semantics to keep in step."""
    import inspect
    for module in (tools_jobs, harness_jobs):
        src = inspect.getsource(module)
        assert "subprocess.Popen" not in src, (
            f"{module.__name__} spawns directly; jobcore.start owns that")


def test_the_lanes_stay_separate():
    assert tools_jobs.LANE != harness_jobs.LANE


# ── what did NOT change (the regression guard) ──────────────────────

def test_the_model_still_gets_eight_jobs():
    """The harness's ceiling is 4. Inheriting it would silently halve
    what a story can run."""
    assert tools_jobs.MAX_JOBS == 8
    assert harness_jobs.MAX_JOBS == 4


def test_bash_background_has_no_budget_unless_asked_for_one():
    """A story running a forty-minute suite today must still run it. A
    budget that arrives by default -- whether as a side effect of the
    merge, or later as a well-meant safety feature -- fails work that
    currently succeeds.

    The behavioural half of this is
    `test_a_long_tool_behaves.py::test_a_job_with_no_timeout_is_still_unbounded`,
    which starts a real job and watches it outlive a default that does
    not exist. This half pins the shape so the default cannot creep back
    in as a constant.
    """
    import inspect
    src = inspect.getsource(tools_jobs.tool_bash_background)
    assert "budget = None" in src, "the unbounded case must be the default"
    assert 'args.get("timeout") is not None' in src, (
        "a budget must be gated on the caller asking for one")


def test_no_budget_means_no_watchdog_thread():
    """`Event.wait(None)` blocks for ever, which would look like a budget
    that never fires and leak a thread per job. The arming is skipped."""
    import inspect
    assert "if budget_s is not None:" in inspect.getsource(jobcore.start)


# ── what the model gained ───────────────────────────────────────────

def test_killing_a_job_kills_its_children():
    """`pip` and `npm` spawn children. Signalling only the shell leaves a
    compiler writing into the workspace -- and worse, the grandchild
    holds stdout open so the drain thread never sees EOF and the job
    reads as running for ever."""
    import inspect
    src = inspect.getsource(jobcore.signal_group)
    assert "killpg" in src
    # And the model's kill path goes through it.
    assert "jobcore.cancel" in inspect.getsource(tools_jobs.tool_bash_kill)


def test_a_grandchild_holding_the_pipe_cannot_wedge_a_job():
    assert jobcore.FINALISE_GRACE_S > 0
    assert "done.wait(FINALISE_GRACE_S)" in __import__(
        "inspect").getsource(jobcore.reap)


def test_the_model_is_told_why_a_job_stopped():
    """A signal death cannot distinguish a budget from a cancel. Without
    the reason, "your suite reached its budget" and "your suite failed"
    are the same sentence."""
    import inspect
    src = inspect.getsource(tools_jobs.tool_bash_output)
    assert "cancelled, not because it" in src
    # THE BUDGET SENTENCE MOVED, and gained the two facts that make it
    # actionable: how long the job ran, and what ceiling it could have
    # asked for. Run 262 is why -- told only "it reached its budget", a
    # model that had watched a 1200s job die at 42% asked for 300 next.
    # So the assertion follows it into `_budget_note` rather than
    # pinning it to the caller.
    assert "_budget_note(snap)" in src
    note = inspect.getsource(tools_jobs._budget_note)
    assert "TIMEOUT, not a failing command" in note


# ── the window is the pod's to set, not the model's ─────────────────

def test_the_output_window_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENTOS_JOB_OUTPUT_MAX", "2048")
    assert tools_jobs._max_output() == 2048


def test_a_nonsense_window_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setenv("AGENTOS_JOB_OUTPUT_MAX", "not-a-number")
    assert tools_jobs._max_output() == tools_jobs.DEFAULT_MAX_OUTPUT


def test_the_window_read_is_compliant_with_the_environ_ban(monkeypatch):
    """`check_no_environ_default` bans the two-argument `os.environ.get`
    across `kernel/`, and a bare `os.environ[...]` would fail a pod that
    simply does not set it."""
    import inspect
    src = inspect.getsource(tools_jobs._max_output)
    assert "in os.environ" in src


def test_the_default_window_is_unchanged():
    assert tools_jobs.DEFAULT_MAX_OUTPUT == 32 * 1024
