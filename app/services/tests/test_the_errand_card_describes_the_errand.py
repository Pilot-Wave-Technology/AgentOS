"""The approval card describes the errand, not a product we do not have.

WHAT IT USED TO SAY:

    "The first run still parks for your plan approval like any other
     errand ... and later runs reuse that same approved plan. Every run
     still asks you before it closes ... You can revoke the grant at any
     time."

Recurrence, in a system where it has never happened. Every grant ever
written is `@once, max_runs=1, run_count=1` -- sixteen of sixteen. So a
person was being asked to agree to something more open-ended than what
they were actually getting, which is the worst direction for a consent
dialogue to be wrong in.

AND THE TASK WAS BURIED. The old body put the bounds first, on the
reasoning that the task is "the part they would have guessed" -- true
when the interesting fact was a recurring grant. With one run and one
approval the only thing worth reading is WHAT IT WILL DO.
"""

from __future__ import annotations

import re

from app.services.errand_proposal_cards import body_for, build_card

ROW = {
    "id": 42, "goal_id": "G", "proposed_by": "GO:ERR11",
    "name": "Run and summarise the test suite",
    "task": "Run the full suite and report every failure.",
    "rationale": "It has not been run since the refactor landed.",
    "sandbox_size": "small", "expires_at": None,
    "cron": "@once", "max_runs": 1,
}


def _body(**kw):
    return body_for(dict(ROW, **kw))


# -- what it no longer claims -------------------------------------------

def test_it_does_not_promise_later_runs():
    b = _body()
    for phrase in ("later runs", "every run", "The first run",
                   "reuse that same approved plan"):
        assert phrase not in b, phrase


def test_it_does_not_mention_a_schedule_or_a_grant_to_revoke():
    """Both describe machinery a person cannot see and does not have."""
    b = _body()
    assert "schedule" not in b.lower()
    assert "revoke" not in b.lower()


def test_it_says_plainly_that_it_runs_once():
    assert "runs once" in _body()


# -- what it says instead ------------------------------------------------

def test_the_task_comes_first():
    """The only thing worth reading is what it will do."""
    b = _body()
    assert b.index("What it will do") < b.index("Approving starts it")


def test_the_task_and_the_reason_are_both_there():
    b = _body()
    assert ROW["task"] in b
    assert ROW["rationale"] in b


def test_the_name_is_in_the_heading():
    assert ROW["name"] in _body()


def test_ownership_is_still_stated():
    """It is their name, their thread and their cost. Shortening the
    card must not drop the part that makes approval mean something."""
    b = _body()
    assert "you own it" in b.lower()
    assert "principal" in b


def test_it_still_says_a_person_closes_it():
    """The one bound that survives: it STARTS without you, it does not
    FINISH without you."""
    b = _body()
    assert "asks you before it closes" in b
    assert "not that it finishes without you" in b


def test_it_offers_asking_for_changes():
    """A card with only approve and decline makes "nearly right" into a
    refusal."""
    assert "stays proposed" in _body()


# -- it has to render ----------------------------------------------------

def test_the_bold_is_balanced():
    """`**a **b** c**` does not render -- the machine size landed inside
    a bold sentence and came out as literal asterisks."""
    b = _body()
    assert "**" not in re.sub(r"\*\*[^*]+\*\*", "", b)


def test_the_machine_is_named_without_nesting_bold():
    assert "`small`" in _body()


def test_a_card_with_no_machine_named_reads_cleanly():
    b = _body(sandbox_size=None)
    assert "runs once." in b
    assert "None" not in b


def test_an_expiry_is_shown_when_there_is_one():
    import datetime as dt
    b = _body(expires_at=dt.datetime(2026, 10, 1, 9, 0))
    assert "lapses if not started" in b


def test_the_buttons_do_not_promise_runs_plural():
    card = build_card(ROW)
    labels = " ".join(o["label"] for o in card["options"])
    assert "these runs" not in labels
    assert "this run" in labels
