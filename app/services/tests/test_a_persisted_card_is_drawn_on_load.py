"""A card in the database is a card on the page, after a reload.

WHAT WAS REPORTED, three times and in three shapes: "plan cards
disappear after I refresh", "the mark story done card is lost after
refresh", "the verification card does not persist".

WHAT WAS ACTUALLY HAPPENING. The goal thread hydrated its cards from
inside a `DOMContentLoaded` listener. That event fires once per HARD
load, so any path where the page's script runs after parsing -- an
htmx-boosted navigation, a swap, a bfcache restore -- registered a
listener for an event that had already passed. The `.card-container`
holders stayed in the DOM as invisible `<script class="card-json">`
tags: correct in the database, correct in the HTML, drawn as nothing.

WHY IT LOOKED LIKE A PERSISTENCE BUG. Cards still ARRIVED live: the
socket path builds its own element with `buildCardElement` and appends
it straight to `#messages`, never touching the hydrator. So the card
was visible until the page reloaded and then gone -- which reads
exactly like it was never saved.

Confirmed from the live DOM: the permission card's `<script
class="card-json">` was present and unprocessed, and calling
`hydrateParkCards()` by hand in the console drew it.

`chat/index.html` has called `hydrateChatCards()` directly since it was
written, which is why the assistant's thread never had this.
"""
from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_the_goal_thread_hydrates_without_waiting_for_an_event():
    """The messages are server-rendered ABOVE the script, so there is
    nothing to wait for -- and waiting made it depend on an event that
    does not always come."""
    src = _read("app/templates/goal_thread/index.html")
    assert "} else {\n  hydrateParkCards();\n}" in src, (
        "hydration is behind an event again; a boosted navigation will "
        "leave every persisted card as an invisible <script> tag")


def test_it_still_waits_when_the_document_is_genuinely_parsing():
    """The one case where waiting is right: if this ever moves above the
    message list, the holders would not exist yet."""
    src = _read("app/templates/goal_thread/index.html")
    assert "document.readyState === 'loading'" in src


def test_older_messages_get_their_cards_too():
    """"Load earlier" re-rendered markdown and never hydrated cards, so
    a card far enough back was never drawn however long you scrolled."""
    src = _read("app/templates/goal_thread/index.html")
    i = src.index("onInserted:")
    block = src[i:i + 1600]
    assert "hydrateParkCards(container)" in block


def test_the_assistant_page_still_calls_it_directly():
    """The page that never had this bug. If it grows a listener, it
    acquires the bug -- named here so the two stay the same shape."""
    src = _read("app/templates/chat/index.html")
    assert "\nhydrateChatCards();" in src, (
        "the assistant's thread now waits for an event too")


def test_the_hydrator_marks_what_it_has_done():
    """The whole approach rests on this: the holder is removed once
    drawn, so re-running over a mixed list is free and a leftover
    <script> tag is proof the hydrator never reached it -- which is how
    this bug was finally located."""
    js = _read("static/js/kernel_park_card.js")
    assert "scriptEl.remove();" in js
    assert "if (!scriptEl) return;" in js


# ── and the cards that were never written at all ─────────────────────

def test_a_question_answered_before_the_poll_still_gets_a_card():
    """THE OTHER HALF, and the one that really was a persistence bug.

    The poster selected `status = 'OPEN'`, so a question settled before
    its next pass never got a card AT ALL. GO settles a plan in about
    thirteen seconds, well inside one pass.

    Measured on ERR17: questions 411, 412, 414 and 415 have card rows;
    413 and 403 -- both `authorization` -- have none and never will,
    because `card_posted_at IS NULL` is a one-shot claim and the status
    had already moved. The plan-approval card is the one GO reliably
    answers within seconds, which is why it was the one that always
    seemed not to persist."""
    src = _read("app/services/kernel_park_cards.py")
    assert "q.status IN ('OPEN', 'ANSWERED')" in src, (
        "a question answered before the poll gets no card, ever")


def test_a_settled_card_renders_without_live_buttons():
    """Otherwise it renders its buttons on every reload, the next click
    409s, and a correct refusal reads as a broken page -- the reasoning
    the errand proposal's own stamp gives."""
    src = _read("app/services/kernel_park_cards.py")
    assert 'card["decided"] = "answered"' in src
    js = _read("static/js/kernel_park_card.js")
    assert "card.needs_human && !card.decided" in js, (
        "the renderer no longer honours `decided`, so the stamp above "
        "buys nothing")


def test_the_claim_protocol_is_untouched():
    """`card_posted_at IS NULL` is what stops a card being posted twice.
    Widening the status filter must not widen that."""
    src = _read("app/services/kernel_park_cards.py")
    assert "AND q.card_posted_at IS NULL" in src


def test_a_finished_run_still_posts_nothing():
    """The other guard stays: a question on a terminal run is waiting on
    nobody, and the inbox learned that from being seven-tenths dead
    cards."""
    src = _read("app/services/kernel_park_cards.py")
    assert "NOT (m.terminal_states @> to_jsonb(e.state))" in src


# ── and a settled card must not offer its buttons again ──────────────

def test_a_card_is_stamped_whoever_answered_it():
    """THE STAMP DEPENDED ON THE ROUTE. `kernel_decide.decide()` writes
    `decided` so the buttons do not come back -- but that is the control
    panel's path, reached from the card's own buttons. GO settles a plan
    with the `decide_plan` TOOL, which goes through the kernel, and the
    kernel cannot write `messages`: that is the app's table and
    `kernel/` may not import `app/`.

    ERR17 question 416: status ANSWERED, card row present, `decided`
    empty. So the plan-approval card renders its buttons for a decision
    already taken and the next click 409s -- a correct refusal that
    reads as a broken page."""
    src = _read("app/services/kernel_park_cards.py")
    assert "SETTLED_CARDS_SQL" in src
    assert "q.status <> 'OPEN'" in src


def test_it_runs_on_every_sweep_and_not_at_the_callers():
    """There is no end to the callers -- the tool, the API, the buttons,
    Slack, the next one. The question's status is the fact; the sweep
    makes the card agree with it."""
    src = _read("app/services/kernel_park_cards.py")
    body = src[src.index("async def sweep()"):]
    assert "_settle_answered_cards(conn)" in body[:400]


def test_stamping_is_idempotent_and_cannot_fail_the_sweep():
    """A card already stamped is not matched, and an unstamped one is
    untidy rather than broken -- not worth losing the pass for."""
    src = _read("app/services/kernel_park_cards.py")
    assert "m.card_data->>'decided' IS NULL" in src
    fn = src[src.index("async def _settle_answered_cards"):]
    fn = fn[:fn.index("async def", 10)]
    assert "except Exception" in fn
