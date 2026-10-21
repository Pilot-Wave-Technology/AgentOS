"""A person may hold a run; an agent must say what made it hold.

THE BUG. `validate_proposal` required every `hold` to carry both `decision` and
`answer`, because the vocabulary was written for the answerer: reading the tree
found a problem, and a hold with no answer behind it did not. Sound rule, wrong
scope. When a person pressed Pause there was no question and no answer to
carry, so every press was refused with E_NO_ANSWER — observed on KER20 runs
1656 and 1657, where nothing reached the mailbox at all.

Synthesising an `answer` to satisfy it would have defeated exactly the check
that caught the mistake, so the rule is now scoped to who is holding:

  agent   still owes the answer that caused it. An agent that can stop work
          and say nothing is a stall with a nicer name.
  person  owes `decision` and nothing else. The justification is that a person
          decided to; there is no answer, and demanding one forces the caller
          to invent it.
"""
from __future__ import annotations

from kernel.mailbox.schema import validate_proposal

KEY = "k-1"
EID = 42


def _hold(actor, **payload):
    return validate_proposal(actor, "hold", payload, KEY, EID)


def test_a_person_may_hold_with_only_a_decision():
    """The KER20 failure. This was E_NO_ANSWER on every press."""
    assert _hold("human:abhishek", decision="I want to look at this") is None


def test_an_agent_still_owes_the_answer_that_caused_it():
    bad = _hold("shca:john-doe", decision="pick one")
    assert bad is not None and bad.code == "E_NO_ANSWER"


def test_an_agent_with_both_is_accepted():
    assert _hold("shca:john-doe", decision="pick one",
                 answer="the tree already has a client") is None


def test_everyone_owes_a_decision():
    """Stopping work without saying what you stopped it for leaves the next
    person nothing to act on — true of a person as much as an agent."""
    for actor in ("human:abhishek", "shca:john-doe"):
        bad = _hold(actor, answer="something")
        assert bad is not None and bad.code == "E_NOTHING_TO_DECIDE", actor
    # Whitespace is not a decision.
    bad = _hold("human:abhishek", decision="   ")
    assert bad is not None and bad.code == "E_NOTHING_TO_DECIDE"


def test_the_human_prefix_is_what_distinguishes_them():
    """`human:` is set by the control-panel endpoint from the logged-in user,
    not by anything a model writes. An actor that merely mentions a person is
    not one."""
    for impostor in ("humanish:x", "shca:human", "agent:human:x", ""):
        bad = _hold(impostor, decision="pick one")
        assert bad is not None, impostor
        assert bad.code in ("E_NO_ANSWER", "E_NO_ACTOR"), impostor


# ── a hold that raced the run out of RUNNING ─────────────────────────────

def test_the_sweep_only_retires_holds_no_leg_will_take():
    """A hold is applied BY A RUNNING LEG. One submitted to a run that has
    already left RUNNING has nobody to apply it, and left PENDING it waits —
    a failed verification sends the run back to RUNNING and the next leg
    pauses it, minutes after the person asked and for a reason they no longer
    remember.

    Observed on KER20 run 1657: Pause was pressed in the same SECOND the
    executor proposed completion. Both acts were correct; they raced.

    Read off the source rather than exercised end to end, because the
    behaviour that matters is WHICH rows it selects — a sweep that also caught
    RUNNING runs would cancel every pause before its leg could take it.
    """
    import inspect

    from kernel.worker.mailbox import expire_stale_holds

    src = inspect.getsource(expire_stale_holds)
    assert "p.kind = 'hold'" in src
    assert "p.status = 'PENDING'" in src
    assert "e.state <> 'RUNNING'" in src, (
        "a RUNNING run's hold must be left for its own leg to take — "
        "sweeping it here would make Pause impossible")
    assert "SUPERSEDED" in src, (
        "it was a real intent that was overtaken, not one that was applied")


# ── the SECOND place the same rule lives ─────────────────────────────────

def test_a_persons_direction_question_needs_no_answer_either():
    """A hold is validated TWICE — once as the intent, once as the question it
    opens — and fixing only the first left every human pause dying at the
    second: "execution 1656 asked badly (E_NO_ANSWER) and was not parked".

    `from` is stamped by `take_proposals` off the proposal ROW, overwriting
    whatever the payload said, so this is not the forgeable payload flag the
    escalation rule two branches above rightly refuses.
    """
    from kernel.mailbox.schema import validate_question

    assert validate_question(
        "direction", "why did you stop me?",
        {"decision": "have a look at this", "from": "human:abhishek"}) is None


def test_an_agents_direction_question_still_owes_the_answer():
    from kernel.mailbox.schema import validate_question

    bad = validate_question("direction", "q", {"decision": "pick one",
                                               "from": "shca:john-doe"})
    assert bad is not None and bad.code == "E_NO_ANSWER"
    # And with no provenance at all — the safe reading is "not a person".
    bad = validate_question("direction", "q", {"decision": "pick one"})
    assert bad is not None and bad.code == "E_NO_ANSWER"


def test_the_hold_actor_is_stamped_from_the_row_not_the_payload():
    """The forgery guard. If this ever read the payload's own `from`, an
    executor could write itself a human's exemption."""
    import inspect

    from kernel.harness.intake import take_proposals

    src = inspect.getsource(take_proposals)
    assert 'hold["from"] = p["actor_id"]' in src, (
        "the hold's provenance must come from the proposal row, not from "
        "anything the executor wrote into the payload")


# ── a pause is an act, not a repeated question ───────────────────────────

def test_each_hold_gets_its_own_identity():
    """`open_request` refuses a question whose TEXT matches one already
    answered — right for an executor asking the same thing twice, wrong for a
    pause. Every pause carries identical words, so the SECOND one on a run was
    refused as a duplicate and the run carried on.

    Observed on KER20 run 1714: four holds ACCEPTED, one park. Only the first
    pause of a run ever worked.
    """
    import inspect

    from kernel.harness.intake import take_proposals
    from kernel.harness.park import apply_hold

    assert 'hold["proposal_id"] = p["id"]' in inspect.getsource(take_proposals)
    src = inspect.getsource(apply_hold)
    assert "content_hash=" in src, (
        "without a per-hold content hash, open_request falls back to matching "
        "question text and every pause after the first is a 'duplicate'")
    assert "proposal_id" in src


def test_a_human_pause_posts_no_park_card():
    """The bar already says paused and Resume is on it. A card restating that
    is a second control for one act, and the two would have to stay in step
    for ever. An answerer's `direction` still gets its card — nobody was
    necessarily watching, and it carries what was asked."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[3].joinpath(
        "app", "services", "kernel_park_cards.py").read_text(encoding="utf-8")
    assert "q.payload->>'from' LIKE 'human:%'" in src
    assert "NOT (q.kind = 'direction'" in src, (
        "only a HUMAN-pressed direction is skipped; an answerer's must still "
        "post, because nobody was necessarily watching that thread")
