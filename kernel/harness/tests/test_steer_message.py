"""A steer is the next instruction, and it may change the plan.

WHAT CHANGED AND WHY. A steer was described as saying *how* and never *what*,
and that description did damage rather than nothing: an instruction that
changed the scope had nowhere to go. A model reading "this does not change what
done means" about "use SQLite instead" has two bad options — set the
instruction aside for being too big, or carry it out while the approved plan
still describes different work. The third option is the right one and nothing
was telling it: fold the instruction into a NEW plan and have a person approve
that.

Plans remain the authority. The steer is authoritative INPUT; a plan is still
how a change becomes real. Saying only the first invites a silent rewrite;
saying only the second makes the instruction unusable. The message has to say
both, which is what these pin.

WHO SAID IT is the other half. A person's instruction and one the assistant
relayed are not the same weight, and the prompt used to attribute every steer
to the assistant — untrue the moment a human could leave one on the Resume
button, and the kind of quiet misreport that teaches a model to discount both.
"""
from __future__ import annotations

from kernel.harness.intake import _steer_message, format_steers_for_prompt


def test_the_instruction_is_the_first_thing_said():
    msg = _steer_message({"instruction": "use the existing helper in utils.py"})
    assert msg["role"] == "user"
    assert "use the existing helper in utils.py" in msg["content"]
    assert msg["content"].startswith("## A new instruction has arrived")


def test_it_says_to_act_and_that_it_supersedes():
    body = _steer_message({"instruction": "x"})["content"]
    assert "Act on it" in body
    assert "supersedes earlier guidance" in body
    # And not to throw away work that already complies.
    assert "Do not redo work" in body


def test_a_scope_change_routes_through_a_new_plan_rather_than_being_refused():
    """The whole point of the change. Both failure modes are named so the
    model cannot pick one: ignoring the instruction, or acting outside the
    approved plan."""
    body = _steer_message({"instruction": "use SQLite instead of Postgres"})["content"]
    assert "CHANGES WHAT DONE MEANS" in body
    assert "register_plan" in body
    assert "not you refusing the instruction" in body
    assert "ignore it because it is bigger than a tweak" in body
    assert "while the approved plan still describes different work" in body


def test_a_human_instruction_is_attributed_to_a_person():
    """`from` comes off the proposal's actor_id, which the submitter cannot
    forge — a payload field could be written by whatever composed the text."""
    body = _steer_message(
        {"instruction": "x", "from": "human:abhishek"})["content"]
    assert "from a person" in body


def test_an_assistant_instruction_is_attributed_to_the_assistant():
    body = _steer_message(
        {"instruction": "x", "from": "shca:john-doe"})["content"]
    assert "from your assistant" in body
    # Unknown or missing provenance must not claim a person said it.
    assert "from your assistant" in _steer_message({"instruction": "x"})["content"]


def test_standing_steers_name_their_source_and_keep_the_plan_rule():
    """These are reloaded at the start of EVERY leg, so the plan rule has to
    survive there too — a steer read on leg 5 must still know what to do with
    a scope change."""
    out = format_steers_for_prompt([
        {"text": "drop is_zero()", "actor": "human:abhishek"},
        {"text": "use the shared client", "actor": "shca:john-doe"},
    ])
    assert "(from a person) drop is_zero()" in out
    assert "(from your assistant) use the shared client" in out
    assert "register_plan" in out
    assert "not a reason to set the instruction aside" in out
    # The old wording attributed everything to the assistant.
    assert "Standing instructions from the assistant" not in out


def test_the_later_steer_wins_is_still_stated():
    """Two instructions that conflict need an order, and the model needs to be
    told which — otherwise it picks by salience."""
    out = format_steers_for_prompt([{"text": "a", "actor": ""},
                                    {"text": "b", "actor": ""}])
    assert "the later one wins" in out
    assert out.index("1. ") < out.index("2. ")      # oldest first
