"""The check-in must compare the answer with the APPROVED PLAN.

WHY THE CHECK-IN EXISTS AT ALL. Every other wake fires because something
happened -- a park, a failed leg, a job gone silent. The scheduled
check-in exists for the one case that raises no event whatsoever: a run
executing its plan perfectly and heading somewhere nobody wanted.
Nothing in the platform can detect that.

AND NOTHING TOLD GO TO LOOK FOR IT. The sequence said "decide from the
answer, and say what you learned", which GO obeyed exactly: on run 309
it asked seven times, every question a variant of "how many tests have
completed", and reported the counts. Faultless status reporting, and
zero evaluation against what was approved -- the failure mode the
check-in was built to catch, going uncaught by the check-in.

The steering doctrine was already there and already good: `steer` gives
an order, a steer that changes what DONE means makes the run re-plan and
park, say what you steered and why. What was missing was the sentence
connecting the answer to it.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
MD = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()


def _row(**kw):
    base = {"id": 1, "execution_id": 309, "story_id": "s", "asked_by": "go",
            "question": "How is it going?", "answer": "Scraping, not the API."}
    base.update(kw)
    return base


def _section() -> str:
    sec = MD[MD.index("On that check-in"):]
    return sec[:sec.index("Silence means nothing is wrong")]


# -- the prompt --------------------------------------------------------

def test_the_prompt_says_to_test_against_the_plan():
    assert "Test the answer against the approved plan" in _section()


def test_it_says_why_only_go_can_catch_this():
    """Without the reason it reads as one more box to tick. The reason
    is that no event exists for this and nothing else is looking."""
    sec = _section()
    assert "raises no event" in sec or "no event at all" in sec
    assert "Only you can" in sec


def test_it_gives_the_three_outcomes():
    sec = _section()
    assert "matches the plan" in sec
    assert "drifted" in sec
    assert "`steer` it back" in sec


def test_a_replan_is_named_as_correct_and_not_a_failure():
    """A steer that changes what DONE means sends the run back to park
    for approval. An agent that reads that as breaking something will
    avoid the steer that matters most."""
    assert "not a failure" in _section()


# -- the two wake texts ------------------------------------------------

def test_the_check_in_wake_carries_the_same_test():
    src = io.open(ROOT / "app/services/go_errand_watch.py",
                  encoding="utf-8").read()
    # THE PROCEDURE MOVED TO THE PROMPT and the wake keeps one
    # imperative -- which still has to name the comparison, because
    # that is the step the check-in exists for.
    assert "against the approved plan" in src
    # Wrapped across string literals in the builder, so assert on
    # a fragment that does not cross one.
    assert "it back if it has drifted" in src


def test_the_answer_wake_carries_it_too():
    """This is the wake that arrives holding the answer, so it is the
    one moment the comparison can actually be made."""
    from app.services.errand_answer_wake import _body

    out = _body(_row())
    assert "against the approved plan" in out
    assert "steer` it back" in out


def test_matching_the_plan_is_an_explicit_outcome():
    """Without it, "nothing to do" has no shape and GO either says
    nothing -- which is what made run 308 silent -- or invents a
    concern to justify the turn."""
    from app.services.errand_answer_wake import _body

    assert "Matches the plan" in _body(_row())
    # The report contract is its own paragraph now, in every wake,
    # rather than a clause on the end of each imperative.
    assert "One line is enough" in _body(_row())


def test_the_answer_wake_still_offers_the_other_two_exits():
    from app.services.errand_answer_wake import _body

    out = _body(_row())
    assert "run_control" in out and "escalate" in out
