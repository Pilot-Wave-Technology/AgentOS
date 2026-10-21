"""Pause and resume act on one run, and take the panel's own text.

WHAT WAS WRONG WITH THE OLD SHAPE. Slack exposed `/goal pause` and
`/goal resume`, which froze dispatch for the WHOLE GOAL -- every story
in it -- while the panel's Executor bar offered pause, resume and
cancel for the one run in front of you. The blunt instrument was the
one reachable from chat and the precise one was not, so the thing
people actually wanted could only be done from the web.

AND THE TEXT IS PART OF THE ACTION, not decoration. The panel asks for
it in a dialog and all three are optional, but they are three
different things:

  PAUSE   a reason, recorded with the hold
  CANCEL  a reason, recorded on a run that is over either way
  RESUME  NOT a note -- a STEER, read before the executor's next model
          call and kept in front of the model for the rest of the run

So a resume's box is the one that can change what the run does, and
its wording is lifted from `run_controls.js` word for word. Two
descriptions of one action is how people come to believe they are two
actions.
"""
from __future__ import annotations

import inspect
import json

import pytest

from app.channels.slack import run_modals


# ── the goal-level pair is gone ──────────────────────────────────────

def test_goal_no_longer_pauses_everything():
    from app.channels.slack.commands import goal
    src = inspect.getsource(goal)
    assert 'sub == "pause"' not in src
    assert 'sub == "resume"' not in src


def test_the_goal_hint_does_not_advertise_them():
    """The hint is what Slack shows in the command picker, and it is
    generated into the manifest — leaving them listed would offer a
    subcommand that answers "unknown"."""
    from app.channels.slack.commands.registry import COMMANDS
    goal = [c for c in COMMANDS if c.name == "goal"][0]
    assert "pause" not in goal.hint
    assert "resume" not in goal.hint
    assert "pause" not in goal.usage


def test_run_advertises_them_instead():
    from app.channels.slack.commands.registry import COMMANDS
    run = [c for c in COMMANDS if c.name == "run"][0]
    assert "pause" in run.hint and "resume" in run.hint
    assert "/run pause" in run.usage and "/run resume" in run.usage


def test_run_accepts_the_new_subcommands():
    """`/run <anything-else>` falls through to status, so a subcommand
    missing from this tuple is silently treated as a story id."""
    src = inspect.getsource(
        __import__("app.channels.slack.commands.run", fromlist=["handle"]))
    i = src.index('if sub not in (')
    guard = src[i:i + 120]
    for name in ("pause", "resume", "cancel", "more", "retry"):
        assert f'"{name}"' in guard, name


# ── the box, and what it says ────────────────────────────────────────

@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_every_action_builds_a_usable_view(action):
    view = run_modals.build_view(action, "t-1", "G1", channel_id="C1")
    assert view["callback_id"] == run_modals.CALLBACK_ID
    assert view["blocks"][-1]["block_id"] == "reason"
    assert len(view["title"]["text"]) <= 24, "Slack truncates past 24"


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_the_text_is_optional(action):
    """An input block is REQUIRED by default. Without `optional` Slack
    refuses a blank submit — and a blank resume is the ordinary case."""
    block = run_modals.build_view(action, "t-1", "G1")["blocks"][-1]
    assert block.get("optional") is True, (
        f"{action} demands text; the panel treats it as optional")


def test_resume_says_the_text_is_a_steer():
    """It is not a note. Somebody typing into it is giving the executor
    its next instruction, and it can change the plan — the dialog has
    to say so before they press the button, not after."""
    text = json.dumps(run_modals.build_view("resume", "t", "G")).lower()
    assert "steer" in text
    assert "next model call" in text


def test_cancel_says_it_cannot_be_undone():
    text = json.dumps(run_modals.build_view("cancel", "t", "G")).lower()
    assert "cannot be undone" in text


def test_pause_says_nothing_is_lost():
    """People do not pause things they fear losing. The panel says the
    sandbox stays up and the journal is kept; Slack should too."""
    text = json.dumps(run_modals.build_view("pause", "t", "G")).lower()
    assert "nothing is lost" in text
    assert "step boundary" in text


def test_the_view_carries_what_the_submission_needs():
    meta = json.loads(run_modals.build_view(
        "pause", "t-9", "G9", channel_id="C9")["private_metadata"])
    assert meta == {"action": "pause", "thread_id": "t-9", "goal_id": "G9",
                    "channel_id": "C9"}


def test_an_unknown_action_builds_nothing_rather_than_guessing():
    with pytest.raises(KeyError):
        run_modals.build_view("explode", "t", "G")


# ── the box is never the only way ────────────────────────────────────

@pytest.mark.asyncio
async def test_no_trigger_means_no_modal():
    """`trigger_id` is valid for three seconds, single use, and absent
    when Slack retries. A surface that only works when the network is
    quick is not a surface."""
    assert await run_modals.open_run_modal(
        None, team_id="T", trigger_id="", action="pause",
        thread_id="t", goal_id="G") is False


@pytest.mark.asyncio
async def test_a_run_with_no_thread_opens_nothing():
    assert await run_modals.open_run_modal(
        None, team_id="T", trigger_id="x", action="pause",
        thread_id="", goal_id="G") is False


def test_inline_text_still_acts_without_a_modal():
    """`/run pause because X` must not open a dialog to ask what it has
    already been told, and must still work when the open fails."""
    src = inspect.getsource(
        __import__("app.channels.slack.commands.run", fromlist=["handle"]))
    i = src.index("open_run_modal")
    before = src[:i]
    assert "not rest.strip()" in before[-400:], (
        "the modal opens even when the text was typed inline")
    after = src[i:i + 600]
    assert "kernel_run_control import" in after, (
        "a failed modal open leaves no path that actually pauses")


# ── the submission ───────────────────────────────────────────────────

def test_the_submission_is_routed():
    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_view_submission)
    assert "_handle_run_control_submission" in src
    assert hasattr(actions, "_handle_run_control_submission")


def test_the_submission_refuses_nothing_for_being_blank():
    """The other modals demand words. These three must not: a pause
    with no reason is a perfectly good pause."""
    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_run_control_submission)
    assert "if not text" not in src
    assert "Please say" not in src


def test_the_submission_checks_goal_membership():
    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_run_control_submission)
    assert "get_goal_role" in src, (
        "a modal submission reaches the run controls with no membership "
        "check — the button path checks, so this is a way round it")


def test_a_refusal_from_the_state_machine_is_shown():
    """"nothing is running", "already paused" are answers. Closing the
    dialog silently would look like it worked."""
    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_run_control_submission)
    assert 'out.get("error")' in src
    assert '"response_action": "errors"' in src


def test_it_calls_the_same_services_the_panel_does():
    """Not a reimplementation. The kill switch and the state machine
    live in those functions, and a second path around them is how they
    get bypassed."""
    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_run_control_submission)
    assert "from app.services.kernel_run_control import cancel, pause, resume" in src


def test_the_decision_is_recorded():
    """The reply is ephemeral — visible to one person, in Slack, never
    persisted. Without a durable trace nobody reading the thread in the
    panel can see who paused it or why."""
    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_run_control_submission)
    assert "record_decision" in src


# ── the plumbing that makes a modal possible from a slash command ────

def test_trigger_id_reaches_the_handler():
    """Slack sends it on every slash command; we simply were not
    reading it, so no command could open a dialog."""
    from app.channels.slack.commands.registry import CommandContext
    assert "trigger_id" in CommandContext.__dataclass_fields__

    import app.channels.router as router
    src = inspect.getsource(router)
    assert '_first("trigger_id")' in src


def test_other_adapters_are_not_handed_an_argument_they_lack():
    """This router serves every platform. Passing `trigger_id`
    unconditionally would break the adapters that do not take it."""
    import app.channels.router as router
    src = inspect.getsource(router)
    assert "signature(" in src and "parameters" in src
