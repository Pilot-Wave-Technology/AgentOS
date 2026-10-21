"""Step 20 -- the prompts and the tool surface, reconciled and pinned.

THE DRIFT THIS PREVENTS, and it has happened in both directions. A tool
added to a profile and never mentioned in its prompt is a tool the model
does not use -- `bash_background` sat in the coding surface while the
prompt said nothing, and legs died polling. A tool NAMED in a prompt and
absent from the surface is worse: chain stage 2 is fail-closed, so the
model is told to call something that is refused by name.

And a prompt can go stale without either list moving: the plan card's
dry-run sentence stayed true-sounding for months after F11 was retired.

So each of the four executor profiles is checked against its own
surface, and the assistant and GO against the facts that moved under
them.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]

PROFILES = ("story-coding", "story-business", "story-coding-review",
            "errand-ops")

# The tools this phase added. A profile either holds them all or holds
# none of the background set -- a surface with `bash_background` and no
# `bash_wait` is the polling trap with no way out of it.
LONG_WORK = ("bash_background", "bash_wait", "bash_output", "bash_jobs",
             "bash_kill")


def _surface(name: str) -> set:
    from kernel.profiles.loader import load_all

    for key, profile in load_all().items():
        if "-".join(p for p in key if p) == name:
            return set(getattr(profile, "tools", None) or profile.added_tools)
    raise AssertionError(f"no profile {name}")


def _prompt(name: str) -> str:
    return (ROOT / "profiles" / name / "prompt.md").read_text(encoding="utf-8")


# -- the surfaces are coherent -----------------------------------------

@pytest.mark.parametrize("name", PROFILES)
def test_a_profile_that_can_start_a_job_can_wait_for_one(name):
    """`bash_background` without `bash_wait` is the polling trap with no
    way out: a step per poll, and the leg ends `stalled` on the eighth
    identical one."""
    tools = _surface(name)
    if "bash_background" not in tools:
        return
    missing = [t for t in LONG_WORK if t not in tools]
    assert not missing, f"{name} can start a job but not {missing}"


def test_every_profile_that_plans_can_run_the_checks_it_planned():
    """AN ERRAND PLANS TOO, which is the thing easy to get wrong here:
    `requires_plan` is true and it holds `register_plan`. Its checks are
    "almost always artifacts", measured mechanically at the claim with
    nothing to run -- but "almost always" is not "only", its own prompt
    tells it how to write a COMMAND check, and that check meets the same
    120-second ceiling a coding story's does."""
    from kernel.profiles.loader import load_all

    profiles = {"-".join(p for p in k if p): v for k, v in load_all().items()}
    for name in ("story-coding", "errand-ops"):
        assert getattr(profiles[name], "requires_plan", False), name
        assert "register_plan" in _surface(name), name
        assert "run_check" in _surface(name), (
            f"{name} plans its own checks and cannot run one")


def test_a_profile_with_no_shell_does_not_gain_one_through_run_check():
    """`run_check` RUNS A COMMAND, so it belongs only where running
    commands already does.

    The reviewer judges a plan it did not write and holds nothing that
    executes. story-business plans and has artifact checks, but has no
    `bash` at all -- "No story_commit and no bash: there is no
    repository to commit to" -- so giving it this would be handing a
    shell to a profile built without one, through a door labelled
    something else.
    """
    for name in ("story-coding-review", "story-business"):
        tools = _surface(name)
        assert "bash" not in tools, name
        assert "run_check" not in tools, (
            f"{name} has no shell and would gain one through run_check")


def test_the_reviewer_still_holds_nothing_that_runs():
    """The reviewer cannot change the thing it is judging because it
    holds no tool that could -- and the new tools must not be the
    exception that quietly re-opens that."""
    tools = _surface("story-coding-review")
    for tool in (*LONG_WORK, "bash", "run_check", "write", "edit"):
        assert tool not in tools, f"the reviewer gained {tool}"


# -- every tool a prompt names is a tool the profile has ---------------

@pytest.mark.parametrize("name", ("story-coding", "errand-ops"))
def test_no_prompt_names_a_tool_its_profile_is_refused(name):
    """Chain stage 2 is fail-closed: a tool named here and absent there
    is refused BY NAME, and the model has been told to call it."""
    tools = _surface(name)
    body = _prompt(name)
    for tool in (*LONG_WORK, "run_check"):
        if re.search(rf"`{tool}[`(]", body):
            assert tool in tools, f"{name}/prompt.md names {tool}, absent"


# -- and the prompts teach the loop that stops the stalls --------------

@pytest.mark.parametrize("name", ("story-coding", "errand-ops"))
def test_the_prompt_says_wait_and_says_not_to_poll(name):
    """The whole point. A leg died on the eighth poll of a twelve-minute
    build; the fix is a tool AND a sentence."""
    body = _prompt(name)
    assert "bash_wait" in body, f"{name} never mentions bash_wait"
    low = body.lower()
    assert "one step" in low
    assert "stalled" in low, (
        f"{name} does not say what polling in a loop costs")


@pytest.mark.parametrize("name", ("story-coding", "errand-ops"))
def test_the_prompt_says_where_the_whole_log_is(name):
    """A poll answers "what is new"; the spill file answers "what
    happened", and a model that does not know it exists loses the
    thousands of lines before the failure."""
    from kernel.bridge import jobspill

    assert jobspill.SPILL_DIR in _prompt(name), (
        f"{name} never says the full output is on disk")


def test_the_coding_prompt_teaches_run_check_and_its_budget():
    body = _prompt("story-coding")
    assert "run_check" in body
    assert "the plan did" in body or "plan declared" in body, (
        "the model must know it does not choose the budget")
    assert "exact tree" in body, (
        "it must know a check already run over this tree is not re-run")


def test_the_coding_prompt_no_longer_promises_a_dry_run():
    """F11's dry run was retired. A prompt that still says it enforces
    the goal/guard labels tells the model something checks its plan when
    nothing does."""
    body = _prompt("story-coding")
    for lie in ("the dry-run enforces", "dry-run enforces their meaning"):
        assert lie not in body, lie


def test_no_prompt_tells_the_model_to_kill_a_job_on_an_amendment():
    """A8. The instruction usually does not touch the running job, and
    killing it throws away the one measurement nobody can cheaply get
    back."""
    from kernel.harness.intake import format_steers_for_prompt

    text = format_steers_for_prompt([{"actor": "human:x", "text": "also X"}])
    assert "not by itself a reason to stop a job" in text


# -- the assistant ------------------------------------------------------

def test_the_assistant_knows_silence_can_mean_a_job_is_running():
    """A forty-minute suite journals nothing, so "no recent steps" alone
    reads as stuck on a story doing exactly what it should."""
    body = (ROOT / "kernel/assistant/AGENTS.md").read_text(encoding="utf-8")
    assert "`jobs`" in body
    assert "last_byte_s_ago" in body, (
        "the assistant cannot separate a slow compile from a pod that "
        "vanished without saying the number")


def test_the_assistant_tool_actually_returns_what_the_prompt_promises():
    """A prompt naming a field the tool does not return is the same
    defect as a prompt naming a tool the profile lacks."""
    import inspect

    from kernel.assistant import progress

    src = inspect.getsource(progress)
    assert '"jobs": await _jobs' in src
    assert '"last_byte_s_ago"' in src
    assert '"running_for_s"' in src


# -- GO -----------------------------------------------------------------

def test_go_no_longer_slices_stories_around_a_clock_that_is_gone():
    body = (ROOT / "kernel/go/AGENTS.md").read_text(encoding="utf-8")
    assert "Neither is how long its commands take" in body


def test_go_knows_a_quiet_story_may_be_working():
    body = (ROOT / "kernel/go/AGENTS.md").read_text(encoding="utf-8")
    assert "journalled nothing" in body
    assert "last byte" in body.lower()
