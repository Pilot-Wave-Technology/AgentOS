"""A steer must reach a model blocked in `bash_wait`, and a budget kill
must teach the model something.

BOTH ARE REGRESSIONS THAT ARRIVED WITH GOOD CHANGES. `bash_wait` as one
step is what makes a two-hour job affordable; it also made the model
unreachable for exactly as long as the job was worth interrupting.
Giving `bash_background` a caller-chosen budget is right; saying nothing
when it fires left the caller guessing, and the guess went the wrong
way.
"""
from __future__ import annotations

import inspect
import pathlib
import sys

import pytest

_BRIDGE = str(pathlib.Path(__file__).resolve().parents[3]
                / "kernel" / "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)
tools_jobs = pytest.importorskip("tools_jobs")
from kernel.harness import executor


# ── 4. the escape hatch ───────────────────────────────────────────────

def test_the_wait_loop_peeks_at_the_mailbox_every_slice():
    """Run 262: a steer was sent while a suite ran, the worker went
    quiet for eight minutes, and it landed only when the job was killed
    from outside -- the first step boundary since the wait began."""
    src = inspect.getsource(executor.BridgeToolExecutor._wait_for_job)
    assert "_interrupted()" in src, (
        "the wait no longer checks the mailbox, so a steer cannot reach "
        "a model waiting on a long job")


def test_the_peek_does_not_drain_the_mailbox():
    """`take_proposals` is the only thing that may apply an intent.
    Draining in two places is how one gets applied twice or lost."""
    src = inspect.getsource(executor.BridgeToolExecutor._interrupted)
    assert "pending_proposals(" in src
    # CALLS, not mentions -- the docstring names both of these to say it
    # does not use them, and a test that cannot tell the difference
    # would forbid explaining itself.
    assert "take_proposals(" not in src
    assert "mark_proposal(" not in src


def test_the_peek_is_false_on_any_doubt():
    """A wait that ended because a read hiccupped would turn a two-hour
    job into a leg boundary for nothing."""
    src = inspect.getsource(executor.BridgeToolExecutor._interrupted)
    assert "return False" in src
    assert "except Exception" in src


def test_the_early_return_says_the_job_is_still_running():
    """Otherwise it reads as a failure and the natural response is to
    start the job again."""
    src = inspect.getsource(executor.BridgeToolExecutor._wait_for_job)
    assert "NOT stopped" in src
    assert "bash_wait" in src.split("NOT stopped")[1][:400], (
        "the model is not told how to resume the wait")


def test_both_job_profiles_explain_the_early_return():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    for name in ("story-coding", "errand-ops"):
        body = (root / "profiles" / name / "prompt.md").read_text(
            encoding="utf-8")
        # WHITESPACE COLLAPSED, because prose is hard-wrapped and the
        # first version of this looked for "returns early" in a file
        # where the wrap fell between the two words. A guard that
        # depends on where a line breaks fails on a reflow and tells
        # you nothing about the prompt.
        flat = " ".join(body.split())
        assert "returns early" in flat, name
        assert "NOT stopped" in flat, name


# ── 8. the budget kill that taught nothing ────────────────────────────

def _snap(**over):
    base = {"id": "job-1", "budget_s": 300, "started_at": 1000.0,
            "finished_at": 1300.0, "ended": "budget"}
    base.update(over)
    return base


def test_the_kill_names_the_budget_and_the_ceiling():
    """Having watched a 1200s job die at 42%, the model asked for 300 --
    a quarter of what was already proved insufficient. It was told "it
    reached its budget" and nothing else."""
    said = tools_jobs._budget_note(_snap())
    assert "300s budget" in said
    assert "300s" in said and "7200" in said, said
    assert "TIMEOUT" in said


def test_it_says_how_long_the_job_actually_ran():
    assert "300s" in tools_jobs._budget_note(_snap())


def test_a_job_already_at_the_ceiling_is_not_told_to_ask_for_more():
    said = tools_jobs._budget_note(_snap(budget_s=7200,
                                         finished_at=8200.0))
    assert "You may ask for up to" not in said, said


def test_a_budget_nobody_asked_for_still_reads_honestly():
    said = tools_jobs._budget_note(_snap(budget_s=None))
    assert "TIMEOUT" in said and "budget" in said


# ── 9. the mirror row that never finished ─────────────────────────────

def test_bash_jobs_carries_every_known_job_not_only_the_live_ones():
    """A job started and never waited on got a start write and no finish
    write, so the row claimed `running = true` for ever. Run 262:
    job-8e62ccf4 read as running for half an hour after the sandbox
    logged exit_code=4 elapsed_s=0.9."""
    src = inspect.getsource(tools_jobs.tool_bash_jobs)
    assert "jobcore.known(LANE)" in src, (
        "bash_jobs is back to live jobs only, so an abandoned mirror row "
        "has nothing to correct it")
    assert "jobs=facts" in src


def test_the_executor_reconciles_from_that_list():
    assert hasattr(executor.BridgeToolExecutor, "_mirror_jobs")
    src = inspect.getsource(executor.BridgeToolExecutor._mirror_jobs)
    assert '"jobs"' in src
