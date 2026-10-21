"""A question a person can see is a question a person can answer.

WATCHED ON STORY dc11d7f2, run 188. The executor asked a question --
`ask_human`, a `clarification` park. Then:

  the assistant answered it with a `steer`, which applies at a step
  boundary and a PARKED run has none, so proposal 53 sat PENDING for
  ever and nothing said so

  the assistant told the person to click the card

  the card had nothing to click

  the run was cancelled to get out of it, and the kernel closed the
  question with `answered_by: 'kernel'`, text "the run ended before
  this was answered"

WHY THE CARD WAS EMPTY, and it was a reasonable-looking decision.
`clarification` is deliberately outside `HUMAN_ANSWERED_KINDS` -- "an
assistant may answer one, which is what makes ask_human cheap" -- and
the card builder read that as "no human control needed": no `options`
branch, and `allow_custom` keyed on `needs_human`. So no buttons and no
text box.

MAY IS NOT MUST. The exclusion says an assistant is ALLOWED to answer,
not that a person is forbidden. Keyed on it, "the assistant can help"
became "you cannot reply", and the only exit from the park was
cancelling the run.

The file already carried this exact lesson about a different kind:
"`permission` was missed here when the kind was added, which made every
destructive-command park unanswerable ... the client renders no buttons
at all". Same failure, one kind over.
"""
from __future__ import annotations

import pytest

from app.services.kernel_park_cards import build_card
from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS


def _row(kind, **kw):
    base = {"kind": kind, "question_text": "may I write here?",
            "execution_id": 188, "id": 68, "goal_id": "G",
            "story_id": "dc11d7f2", "payload": {}, "checks": "[]"}
    base.update(kw)
    return base


def test_a_clarification_offers_a_way_to_answer():
    """THE FIX. Without it the only exit is cancelling the run."""
    card = build_card(_row("clarification"), None, "")
    assert card["options"], (
        "no buttons on a card that parks a run -- the person can read "
        "the question and has no way to reply to it")


def test_and_a_box_to_type_the_answer_in():
    # A clarification IS free text. There is no verdict to press.
    card = build_card(_row("clarification"), None, "")
    assert card["allow_custom"] is True, (
        "`allow_custom` keyed on `needs_human`, which is False for a "
        "clarification because the ASSISTANT may answer it -- not "
        "because a person may not")


def test_the_assistant_may_still_answer_it():
    """The exclusion stays. It is what makes `ask_human` cheap: the
    assistant relays an answer without waiting for a person, and only
    the kinds that genuinely need a human are reserved."""
    assert "clarification" not in HUMAN_ANSWERED_KINDS
    assert build_card(_row("clarification"), None, "")["needs_human"] is False


@pytest.mark.parametrize("kind", sorted(HUMAN_ANSWERED_KINDS))
def test_every_kind_that_parks_a_run_can_be_answered(kind):
    """The general rule, so the next kind added does not repeat this.

    `permission` was missed exactly this way when it was added, and the
    comment recording that is four lines above the code that missed
    `clarification`.
    """
    card = build_card(_row(kind), None, "")
    assert card["options"] or card["allow_custom"], (
        f"a {kind} park renders with no control at all, so the run can "
        f"only be left by cancelling it")


def test_a_clarification_needs_no_human_uid_to_resolve():
    """The button must not lead to a refusal.

    `resolve_request` reserves approval to a person for plan,
    acceptance, escalation and permission. A clarification is
    deliberately not in that guard, so submitting one works -- asserted
    here because a control that always errors is worse than none.
    """
    import inspect

    from kernel.harness import park
    src = inspect.getsource(park.resolve_request)
    guard = src[src.index("E_APPROVAL_NOT_HUMAN") - 600:
                src.index("E_APPROVAL_NOT_HUMAN")]
    assert "clarification" not in guard
