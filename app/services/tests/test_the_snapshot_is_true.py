"""The snapshot the session reads every turn has to be TRUE.

`story_state_snapshot` is injected before each of the agent's turns and is the
only live account it gets of where the story is. Two things in it were false
after the DAG was removed, and one of them pointed the agent the wrong way at
the single most important moment:

  * `executing` resolved to `next_action: "complete"`. That was a DAG-era
    safety net, reached only when every task was terminal. There is no DAG:
    `approve_local_plan` sets `executing` the moment a person approves, so the
    very next snapshot said "complete" and the kickoff then asked the session
    to summarise work it had not started. The transition that should say GO
    said STOP.

  * `awaiting_review` and `awaiting_next` BOTH said "complete", so a hint
    existed to tell them apart -- prose correcting a value that could have
    carried the distinction itself.

A freshly accepted story also has no `current_phase` at all (checkout does not
set one), resolved to `planning`, and got an EMPTY hint -- on exactly the turn
where the session most needs telling it may not write code yet.
"""
from __future__ import annotations

from app.services.local_dispatch import (_LIFECYCLE_ACTION, _lifecycle_action,
                                         _phase_hint)

# Vocabulary the control plane can produce. The extension's kickoff switches on
# these (src/session.ts `kickoffFor`), so a value added here without a branch
# there falls through to the generic opener.
ACTIONS = {"discovery", "planning", "awaiting_approval", "execute",
           "submitting", "awaiting_merge", "awaiting_closeout",
           "integrating", "paused"}


def test_approving_a_plan_does_not_say_the_work_is_complete():
    assert _lifecycle_action("executing") == "execute", (
        "the snapshot tells the session the work is finished the moment the "
        "person approves the plan"
    )


def test_every_phase_resolves_to_a_known_action():
    for phase, action in _LIFECYCLE_ACTION.items():
        assert action in ACTIONS, f"{phase} resolves to unknown action {action!r}"


def test_the_phases_a_person_waits_on_are_distinguishable():
    # `awaiting_review` and `awaiting_next` BOTH meant "complete" and needed
    # prose to tell them apart. They are retired: with no diff review they
    # described the same moment. What replaced them says who is waiting on
    # whom -- `submitting` is the extension's turn, `awaiting_merge` is the
    # person's -- and they must not collapse back into one answer.
    assert (_lifecycle_action("submitting")
            != _lifecycle_action("awaiting_merge"))


def test_the_retired_phases_resolve_to_the_safe_default():
    # A story mid-flight across the change, or an older extension, must not
    # be told to execute on a phase nobody writes any more.
    for dead in ("awaiting_review", "awaiting_next"):
        assert _lifecycle_action(dead) == "planning"


def test_a_story_with_no_phase_is_still_told_not_to_write_code():
    assert _lifecycle_action("") == "planning"
    hint = _phase_hint("")
    assert hint, "a freshly accepted story gets no hint at all"
    assert "do not build" in hint.lower()


def test_every_phase_carries_a_hint():
    for phase in _LIFECYCLE_ACTION:
        assert _phase_hint(phase), f"{phase} has no hint"


def test_no_hint_describes_machinery_that_is_gone():
    dead = ("DAG", "task", "claim_next_task", "sentinel")
    for phase in _LIFECYCLE_ACTION:
        hint = _phase_hint(phase)
        for word in dead:
            assert word.lower() not in hint.lower(), (
                f"the {phase} hint still mentions {word}, which does not exist "
                "in the local lane"
            )


def test_the_approved_phase_says_what_is_and_is_not_allowed():
    hint = _phase_hint("executing").lower()
    assert "do the work" in hint
    assert "do not push" in hint, (
        "the one phase where the agent has a working tree and a branch does "
        "not tell it to leave pushing alone"
    )
