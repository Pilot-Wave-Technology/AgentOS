"""A8 -- a job that is already running outlives the approval it started under.

THE CASE. A person amends the plan while a forty-minute suite is
running. `requires_plan` closes the gate again, and with the job tools
gated behind it the leg can no longer poll the job, wait for it, or kill
it: the suite runs on invisibly, its result is unreachable, and the only
exit is the stall clock.

THE DISTINCTION THE GATE NOW DRAWS. Starting work is what a plan
authorises, so `bash_background` stays gated. Watching and stopping work
that is already running begins nothing and reaches nothing new -- the
reach was granted when the job started.

AND THE SECOND HALF IS PROSE, not policy. A model told the plan is no
longer approved will reasonably conclude it should kill the job and
start over, which throws away the one measurement nobody can cheaply get
back. The gate lets it keep watching; the standing-instruction preamble
tells it to.
"""

from __future__ import annotations

from kernel.harness.gate import _OPEN_BEFORE_PLAN, needs_approved_plan


def test_a_running_job_can_still_be_watched_before_a_plan_is_approved():
    for tool in ("bash_output", "bash_wait", "bash_jobs"):
        assert not needs_approved_plan(tool), (
            f"{tool} gated: an amendment mid-job would make the job's "
            f"result unreachable and end the leg on the stall clock")


def test_a_running_job_can_still_be_killed_before_a_plan_is_approved():
    """The person's escape hatch has to work while the gate is closed."""
    assert not needs_approved_plan("bash_kill")


def test_starting_work_still_needs_a_plan():
    """The distinction, and the whole reason the set is not just widened."""
    assert needs_approved_plan("bash_background")
    assert needs_approved_plan("bash")
    assert needs_approved_plan("write")
    assert needs_approved_plan("edit")
    assert needs_approved_plan("story_commit")
    assert needs_approved_plan("run_check"), (
        "run_check runs a command; it is work, and the plan is what "
        "declares the check in the first place")


def test_the_set_still_lists_what_is_permitted_and_not_what_is_forbidden():
    """An omission must be a needless refusal, never a silent hole."""
    assert needs_approved_plan("a_tool_nobody_has_written_yet")
    assert "read" in _OPEN_BEFORE_PLAN and "register_plan" in _OPEN_BEFORE_PLAN


def test_the_gate_says_why_the_job_tools_are_open():
    """The old comment said the opposite; a stale reason invites a revert."""
    src = open("kernel/harness/gate.py", encoding="utf-8").read()
    assert "amendment" in src.lower()
    assert "no job can exist to" not in src, (
        "the old rationale -- bash_background is gated so no job can "
        "exist -- is no longer true and would argue for re-gating these")


def test_an_amendment_does_not_tell_the_model_to_kill_its_job():
    from kernel.harness.intake import format_steers_for_prompt

    text = format_steers_for_prompt(
        [{"actor": "human:abhi", "text": "also handle the empty case"}])
    assert "not by itself a reason to stop a job" in text
    assert "bash_wait" in text
    assert "also handle the empty case" in text


def test_the_person_can_still_ask_for_a_job_to_be_stopped():
    """Not a prohibition -- a default. The instruction can override it."""
    from kernel.harness.intake import format_steers_for_prompt

    text = format_steers_for_prompt([{"actor": "human:abhi", "text": "stop"}])
    assert "where one of these instructions says to" in text
