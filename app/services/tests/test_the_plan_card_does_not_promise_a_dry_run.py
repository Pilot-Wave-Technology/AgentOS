"""The plan card says what is known, and no more.

THE CLAIM WAS TRUE ONCE. While F11's dry run existed, the card could
honestly say the checks "are being run against the current tree to
confirm they can tell the difference" -- something really did run them.
The dry run was retired and the sentence stayed, so every approver since
has been told the checks were exercised when nothing had touched them.

A LIE IN AN APPROVAL PROMPT IS THE EXPENSIVE KIND. The whole point of
parking is that a person decides with the facts in front of them; a
claim about work that did not happen spends the trust the park exists to
use. `cards.py` already carries the rule for the other half of this
(`dryrun_of` WAS HERE AND MUST NOT COME BACK) -- this is the prose side
of the same retirement.

Cheap to check and worth pinning: the next person to reinstate a dry run
should have to change this test deliberately.
"""
from __future__ import annotations

import inspect

from kernel.harness.tools_worker import plan

from app.services.tests.test_a_story_is_always_reviewed import code_only


def _card_text() -> str:
    """The prose the approver reads, with comments stripped.

    `code_only` matters here: the comment above the string explains the
    retired claim and quotes it, so a plain substring search would find
    the words in the explanation of why they are gone.
    """
    return code_only(inspect.getsource(plan))


def test_the_card_does_not_claim_the_checks_were_run():
    src = _card_text()
    for lie in ("are being run against the current tree",
                "confirm they can tell the difference"):
        assert lie not in src, (
            f"the plan card still promises a dry run ({lie!r}). F11's dry "
            f"run was retired; nothing runs the checks at plan time.")


def test_the_card_still_says_how_many_checks_there_are():
    """Honesty is not silence. The count is the useful half."""
    assert "len(checks)" in _card_text()


def test_the_card_says_the_checks_have_not_run():
    """State the negative explicitly, or the reader supplies the old
    assumption from every previous card they have approved."""
    src = _card_text()
    assert "NOT been run" in src


def test_no_checks_is_still_called_out():
    """The other branch is the more dangerous one and must survive."""
    assert "nothing will define done" in _card_text()
