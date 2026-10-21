"""A card that arrives while the socket is down must survive catch-up.

MEASURED ON ERR08, and the reason "the approval only appears after a
refresh" survived three attempts at the wrong layer:

    15:31:36  the user's message is delivered live, 1 watcher
    15:32:31  scheduler-next posts the errand proposal card
    15:33:09  user-ws: disconnected ... Goal thread subscriber added

The card was broadcast during the gap, to nobody -- correctly, since
nobody was there. On reconnect the client calls the catch-up endpoint,
which returned the row WITHOUT `card_data`, so the proposal came back as
a plain bubble: its own prose, no buttons. A reload then "fixed" it,
because the full page render does read `card_data`.

So it was neither a delivery failure nor a render failure, which is
exactly why looking at delivery and rendering explained nothing. The
catch-up path dropped the half of the row that makes a card a card.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _endpoint() -> str:
    src = _read("app/routers/goal_thread.py")
    start = src.index("    # Find the creation time of the anchor message")
    return src[start:src.index("return JSONResponse({\"messages\": out})")]


# -- the server half ---------------------------------------------------

def test_the_catch_up_query_selects_card_data():
    assert "m.card_data," in _endpoint(), (
        "the catch-up query omits card_data, so a card missed while "
        "disconnected can never come back as a card")


def test_it_returns_both_card_shapes():
    """Two kinds of card reach this thread and they render by different
    routes -- the errand proposal server-side, everything else through
    the shared hydrator. Returning one and not the other fixes half the
    bug and leaves the other half looking identical."""
    body = _endpoint()
    assert "proposal_of(card)" in body
    assert "_kernel_card(card)" in body


def test_it_uses_the_same_helpers_as_the_page_render():
    """A third way of describing a card is a third way for it to be
    subtly wrong. `proposal_of` and `_kernel_card` are what
    `_normalize_messages` already calls."""
    src = _read("app/routers/goal_thread.py")
    norm = src[src.index("def _normalize_messages("):]
    norm = norm[:norm.index("return messages")]
    assert "proposal_of(card)" in norm and "_kernel_card(card)" in norm


def test_a_plain_message_is_unchanged():
    """Most caught-up messages have no card, and adding empty keys to
    every one of them would be a silent shape change for the client."""
    body = _endpoint()
    assert "if proposal is not None:" in body
    assert "if kernel_card:" in body


# -- the client half ---------------------------------------------------

def _page() -> str:
    return _read("app/templates/goal_thread/index.html")


def test_the_catch_up_loop_renders_the_card():
    page = _page()
    fn = page[page.index("async function catchUpMissedMessages()"):]
    fn = fn[:fn.index("// ── WebSocket")] if "WebSocket" in fn else fn
    assert "appendCatchUpCard(msg)" in fn, (
        "catch-up appends the text and drops the card")


def test_it_reuses_the_live_builders_and_does_not_reimplement_them():
    """`decideProposal` looks up `proposal-actions-<id>` by id. Markup
    that differs renders buttons that do nothing -- worse than no
    buttons, because it looks like it worked."""
    page = _page()
    fn = page[page.index("function appendCatchUpCard("):]
    fn = fn[:fn.index("async function catchUpMissedMessages()")]
    assert "buildProposalCard(" in fn
    assert "hydrateParkCards(" in fn


def test_one_bad_card_does_not_lose_the_rest_of_the_catch_up():
    """The loop is replaying everything missed while disconnected. A
    throw partway through would drop every message after it, silently,
    on exactly the path that exists to stop messages being lost."""
    page = _page()
    fn = page[page.index("function appendCatchUpCard("):]
    fn = fn[:fn.index("async function catchUpMissedMessages()")]
    assert "try {" in fn and "catch (e)" in fn
    assert "console.error" in fn


def test_the_card_lands_in_the_message_it_belongs_to():
    """Appended to the bubble `appendMessage` just created, keyed by the
    row id, so order is preserved and a card cannot attach itself to the
    wrong message."""
    page = _page()
    fn = page[page.index("function appendCatchUpCard("):]
    fn = fn[:fn.index("async function catchUpMissedMessages()")]
    assert 'querySelector(`[data-msg-id="${msg.id}"]`)' in fn
    assert "bubble.appendChild" in fn
