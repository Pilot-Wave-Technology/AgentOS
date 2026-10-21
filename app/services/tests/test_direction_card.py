"""A held run's card must say it is held.

THE BUG THIS EXISTS TO PREVENT ALREADY HAPPENED ONCE. `needs_human`
covered three question kinds and the card titled all of them "Plan
approval", so an ESCALATION — which asks whether the bar itself is
wrong after repeated refusals — arrived looking like a routine plan to
approve. A person reading the title read the wrong question.

`direction` is the fourth, and it is the one most easily mistaken: the
buttons are approve-shaped, and "approve" here would mean carry on with
the plan already approved, not approve a new one. Titles and labels are
the whole interface at that moment.

Pure functions, no database: `build_card` and `_direction_body` decide
everything asserted here.
"""

from app.services.kernel_park_cards import _direction_body, build_card

ROW = {
    "id": 7, "kind": "direction", "execution_id": 1234,
    "goal_id": "KER12", "story_id": "abc123",
    "question_text": "FastAPI or stay on Flask?",
    "payload": {
        "asked": "shouldn't this be FastAPI?",
        "answer": "The plan picks Flask and two routes are written "
                  "against it.",
        "decision": "Switch to FastAPI, or stay on Flask?",
        "options": "Switching is ~3 files plus the test harness.",
        "why_now": "The next step writes the third route.",
    },
    "dryrun": None,
}


def test_title_is_not_a_plan_approval():
    card = build_card(ROW, None, "body")
    assert card["title"] != "Plan approval"
    assert "Paused" in card["title"]


def test_buttons_offer_carry_on_not_approve_plan():
    labels = [o["label"] for o in build_card(ROW, None, "body")["options"]]
    assert any("Carry on" in x for x in labels)
    assert not any("Approve plan" in x for x in labels)
    # Declining is not a thing here: there is no plan being proposed, and
    # a person who wants the story stopped cancels it.
    assert not any("Decline" in x for x in labels)


def test_a_person_may_type_their_own_decision():
    # Two buttons cannot carry "stay on Flask but pull the validation
    # bit out", which is the answer people actually give.
    assert build_card(ROW, None, "body")["allow_custom"] is True


def test_body_carries_question_answer_and_fork():
    body = _direction_body(ROW["payload"])
    assert "shouldn't this be FastAPI?" in body
    assert "two routes are written" in body
    assert "Switch to FastAPI, or stay on Flask?" in body
    assert "~3 files" in body


def test_body_says_the_work_stopped_and_is_not_lost():
    # "Your story has stopped" is what they read first and it sounds
    # like a failure. It is not: the run is parked exactly where it was.
    body = _direction_body(ROW["payload"])
    assert "stopped" in body
    assert "nothing is lost" in body.lower()


def test_body_says_a_change_means_a_new_plan():
    # The assistant, the executor prompt and this card all have to agree
    # on what happens next, or a person is told three different things.
    body = _direction_body(ROW["payload"])
    assert "new cycle" in body
    assert "approval" in body


ACCEPTANCE = {
    "id": 9, "kind": "acceptance", "execution_id": 1441,
    "goal_id": "KER12", "story_id": "abc123",
    "question_text": "verification passed — accept as done, or describe "
                     "what more this story should do",
    "payload": {}, "dryrun": None,
}


def test_acceptance_asks_about_the_story_not_a_plan():
    """The only card in the set that is not about a plan.

    Watched live: a freshly-pushed acceptance card offered "✓ Approve
    plan / ✎ Request changes / ✕ Decline". Verification had already
    passed, there was no plan in front of anyone, and declining is not
    something you can do to finished work. The browser had two card
    renderers and only the reload path knew about acceptance -- so the
    labels are pinned HERE, at the one place that decides them.
    """
    labels = [o["label"] for o in build_card(ACCEPTANCE, None, "b")["options"]]
    assert any("story" in x.lower() and "done" in x.lower() for x in labels)
    assert any("more work" in x.lower() for x in labels)
    # Both are category errors on work that is already verified.
    assert not any("Approve plan" in x for x in labels)
    assert not any("Decline" in x for x in labels)


def test_every_park_kind_offers_its_own_buttons():
    """No two kinds may share a button set by accident.

    The browser renders whatever `options` says, so this function is the
    single source of truth for what a person is asked. If two kinds ever
    produce identical labels again, one of them is wearing the other's
    clothes.
    """
    seen = {}
    for kind, row in (("direction", ROW), ("acceptance", ACCEPTANCE)):
        labels = tuple(o["label"] for o in build_card(row, None, "b")["options"])
        assert labels, f"{kind} offers no buttons"
        seen[kind] = labels
    assert seen["direction"] != seen["acceptance"]


def test_body_survives_an_empty_payload():
    # A hold is refused without a decision, but the card poster must not
    # be the thing that crashes if one ever gets through.
    body = _direction_body({})
    assert "waiting on you" in body
