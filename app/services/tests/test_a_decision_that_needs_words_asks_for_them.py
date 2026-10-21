"""Revise, decline and "ask for more work" open a box.

WHAT THIS REPLACES, AND WHY IT WAS WORSE THAN IT LOOKED. Pressing
*revise* on a plan in Slack returned a sentence -- "reply in this
channel with what should change, and the assistant will pick it up" --
and `handle_park` returned BEFORE calling `decide`. So:

  * nothing was recorded. The card still waited, while the person who
    pressed the button had every reason to think they had answered it.
  * the reason, once typed, was a loose channel message that something
    else had to associate with a question.
  * and the prompt was ephemeral -- visible only to the presser, never
    persisted -- so a second person found a card nobody had touched.

"Ask for more work" was worse still: the closeout card's own text says
"mark it done, or ask for more work and it starts a new plan", and it
rendered with NO BUTTONS, so the only way to do the thing the card
described was a `/run more <what>` slash command the card never
mentions.
"""
from __future__ import annotations

import json

import pytest

from app.channels.slack import go_modals


# ── which verdicts need words ────────────────────────────────────────

def test_only_the_verdicts_that_need_a_reason_get_a_box():
    assert go_modals.needs_reason("park", "revise")
    assert go_modals.needs_reason("park", "decline")
    assert go_modals.needs_reason("park", "declined")
    assert go_modals.needs_reason("errand", "decline")


def test_approve_is_still_one_click():
    """The point of putting the card in Slack. A dialog on the common
    case is friction for nothing."""
    assert not go_modals.needs_reason("park", "approve")
    assert not go_modals.needs_reason("errand", "approve")


def test_an_unknown_kind_does_not_demand_words():
    """A new button family must not start silently opening modals."""
    assert not go_modals.needs_reason("something_new", "revise")
    assert not go_modals.needs_reason("", "")


def test_the_verdict_is_matched_regardless_of_case_and_space():
    assert go_modals.needs_reason("park", "  Revise ")


# ── the view Slack is actually sent ──────────────────────────────────

def _view(kind="park", subject="42", value="revise"):
    return go_modals.build_view(kind, subject, value,
                                channel_id="C1", message_ts="1.2")


def test_the_view_carries_what_the_submission_will_need():
    """`private_metadata` is the only thing that survives to the
    submission: Slack sends back the view, not the message it came
    from. Anything missing here cannot be recovered later."""
    meta = json.loads(_view()["private_metadata"])
    assert meta == {"kind": "park", "subject_id": "42", "value": "revise",
                    "channel_id": "C1", "message_ts": "1.2"}


def test_the_input_block_id_matches_what_the_reader_reads():
    """A block_id typo gives an empty reason on every submit, and the
    modal reports "please say what should change" to somebody who just
    did. The two are written in different files, so they are pinned."""
    view = _view()
    block = view["blocks"][-1]
    assert block["block_id"] == "reason"
    assert block["element"]["action_id"] == "text"

    submitted = {"state": {"values": {"reason": {"text": {
        "value": "  use the existing helper  "}}}}}
    assert go_modals.read_reason(submitted) == "use the existing helper"


def test_the_box_is_mandatory_at_slacks_end_too():
    """`min_length` makes Slack refuse an empty submit before it ever
    reaches us — one fewer round trip for the obvious mistake."""
    el = _view()["blocks"][-1]["element"]
    assert el["min_length"] >= 3
    assert el["multiline"] is True


def test_the_title_fits_slacks_limit():
    """Slack truncates a modal title over 24 characters without saying
    so, which is how a dialog ends up called "Request chang"."""
    for kind, value in (("park", "revise"), ("park", "decline"),
                        ("errand", "decline")):
        view = go_modals.build_view(kind, "1", value)
        assert len(view["title"]["text"]) <= 24, (kind, value)


def test_an_unrecognised_verdict_still_builds_a_usable_box():
    """Better a generic prompt than a KeyError on a button press."""
    view = go_modals.build_view("park", "1", "something_else")
    assert view["blocks"][-1]["block_id"] == "reason"
    assert view["title"]["text"]


def test_unreadable_metadata_does_not_raise():
    assert go_modals.read_meta({"private_metadata": "not json"}) == {}
    assert go_modals.read_meta({}) == {}


def test_a_missing_state_reads_as_empty_rather_than_raising():
    assert go_modals.read_reason({}) == ""
    assert go_modals.read_reason({"state": {"values": {}}}) == ""


# ── the more-work box ────────────────────────────────────────────────

def test_the_more_work_box_carries_the_story():
    meta = json.loads(go_modals.build_more_view(
        "1af46d5a", channel_id="C9")["private_metadata"])
    assert meta["story_id"] == "1af46d5a"
    assert meta["channel_id"] == "C9"


def test_the_more_work_box_says_it_starts_a_new_plan():
    """It is not a comment box. Somebody typing into it is committing
    the story to another round of work, and the dialog should say so
    before they press the button, not after."""
    view = go_modals.build_more_view("s1")
    text = json.dumps(view)
    assert "new plan" in text.lower()
    assert view["blocks"][-1]["block_id"] == "reason"


def test_both_boxes_read_through_the_same_reader():
    """One `read_reason` serves both, so the block_id must agree."""
    for view in (go_modals.build_view("park", "1", "revise"),
                 go_modals.build_more_view("s1")):
        assert view["blocks"][-1]["block_id"] == "reason"


def test_the_two_callbacks_are_distinct():
    """They route to different handlers; one value for both would send
    a more-work request into the park decider."""
    assert go_modals.CALLBACK_ID != go_modals.MORE_CALLBACK_ID


# ── the closeout card, which had no buttons at all ───────────────────

def test_the_closeout_card_is_decidable_now():
    from app.channels.slack.go_cards import DECIDABLE, REPORTING
    assert "closeout_offer" in DECIDABLE
    assert "closeout_offer" not in REPORTING, (
        "a card in both renders read-only and loses its buttons")


def test_the_closeout_card_actually_carries_a_button():
    """The renderer needs `story_id` and an option list. Without them
    it logs a warning and falls back to read-only — which is exactly
    the state this card was in while its text promised an action."""
    import inspect

    from app.services import story_landing
    src = inspect.getsource(story_landing)
    i = src.index('"kind": "closeout_offer"')
    window = src[i:i + 400]
    assert '"story_id"' in window
    assert '"options"' in window


def test_the_closeout_action_id_round_trips():
    """A story id is short hex, not an integer. Reusing the `(\\d+)`
    pattern of the other two families would never match, and the
    button would be dead with no error anywhere."""
    from app.channels.slack.go_cards import CLOSEOUT_ACTION_RE
    m = CLOSEOUT_ACTION_RE.match("closeout_more_1af46d5a")
    assert m and m.group(1) == "more" and m.group(2) == "1af46d5a"
    assert not CLOSEOUT_ACTION_RE.match("closeout_more_")


def test_the_rendered_button_id_is_one_the_dispatcher_matches():
    """The id is built in go_cards and parsed in actions. A test that
    walks both is the only thing that keeps them in step — a button
    nothing can parse is a dead click and one INFO line."""
    from app.channels.slack.go_cards import CLOSEOUT_ACTION_RE, render
    blocks = render({
        "kind": "closeout_offer", "story_id": "1af46d5a",
        "body": "Every lane has merged.",
        "options": [{"id": "more", "value": "more",
                     "label": "Ask for more work"}]})
    assert blocks, "the closeout card rendered nothing"
    ids = [e["action_id"] for b in blocks
           for e in (b.get("elements") or []) if e.get("action_id")]
    assert ids, "the closeout card rendered without buttons"
    assert all(CLOSEOUT_ACTION_RE.match(i) for i in ids), ids


@pytest.mark.parametrize("fn", ["_handle_go_reason_submission",
                                "_handle_more_work_submission"])
def test_both_submissions_are_routed(fn):
    """A callback_id nothing routes closes the modal and drops the
    text with no error to anyone."""
    import inspect

    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_view_submission)
    assert fn in src
    assert hasattr(actions, fn)


def test_a_refusal_keeps_the_modal_open_with_the_text_in_it():
    """`response_action: errors` is the only reply that does. Anything
    else closes the dialog and the sentence they typed is gone."""
    import inspect

    from app.channels.slack import actions
    for fn in (actions._handle_go_reason_submission,
               actions._handle_more_work_submission):
        src = inspect.getsource(fn)
        assert '"response_action": "errors"' in src, fn.__name__
        # Keyed to the block, or Slack shows the error nowhere.
        assert '"reason"' in src, fn.__name__


def test_the_fallback_survives_a_modal_that_will_not_open():
    """`trigger_id` expires in three seconds and is absent on a Slack
    retry. A decision surface that works only when the network is
    quick is not a decision surface."""
    import inspect

    from app.channels.slack import actions
    src = inspect.getsource(actions._handle_block_actions)
    assert "falling back to the channel prompt" in src
    assert "/run more" in src


def test_a_kind_only_card_still_renders():
    """`blocks.py` reconciles `card_type` and `kind` for its own
    dispatch and then hands `render` the raw card. Reading only
    `card_type` meant every `kind` card fell through to None — which
    is why the closeout card had no buttons no matter what was added
    to DECIDABLE."""
    from app.channels.slack.go_cards import render
    assert render({"kind": "kernel_verdict", "body": "done"})
    assert render({"card_type": "kernel_verdict", "body": "done"})
    assert render({"kind": "not_a_card_we_own", "body": "x"}) is None
