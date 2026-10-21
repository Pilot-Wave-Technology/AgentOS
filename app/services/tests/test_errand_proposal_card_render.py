"""I5f · the proposal card renders WITH BUTTONS in the goal thread.

CAUGHT BEFORE THE LIVE TEST, AND IT IS THE SAME BUG AS Ec3b. There, the
`permission` kind was missing from `HUMAN_ONLY_KINDS`, so a
destructive-command park rendered a card with no buttons and was
unanswerable. Here the card was a brand-new `card_type` posted into the
GOAL thread — a template that had never rendered a card at all, only
markdown. The endpoint worked, the card was written, and a person
reading it had nothing to click.

The autonomy path is unusable without this: `errand_schedules` INSERT is
a human privilege, so the ONLY way a grant is ever created is somebody
pressing this button.

WHY THE GOAL THREAD ANYWAY. There is no story yet and no owner, and the
rule is "any human in the goal may approve" — posting into one person's
assistant would silently pick one. So the thread learns one card type,
and every other message there stays markdown.
"""
from __future__ import annotations

import io
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── the row carries the card at all ──────────────────────────────────────

def test_the_thread_query_selects_card_data():
    """One column list feeds all three render paths — the page, "load
    earlier", and the partial refresh. Adding it anywhere else would be
    two of the three."""
    src = _read("app/routers/goal_thread.py")
    cols = src[src.index("_MESSAGE_COLUMNS"):]
    cols = cols[:cols.index('"""', cols.index('"""') + 3)]
    assert "m.card_data" in cols


def test_asyncpg_returning_jsonb_as_text_is_handled():
    """THE TRAP THIS PROJECT HAS HIT THREE TIMES, and it always fails
    CLOSED — the card reads as absent rather than broken, which is
    exactly how it survives a review."""
    from app.services.goal_thread_cards import proposal_of

    card = {"card_type": "errand_proposal", "proposal_id": 7, "title": "t"}
    assert proposal_of(card) == card, "a dict is not accepted"
    assert proposal_of(json.dumps(card)) == card, (
        "asyncpg returns jsonb as TEXT unless a codec is registered, and "
        "the string form is rejected — so the buttons never render")


@pytest.mark.parametrize("raw", [
    None, "", "{}", "not json at all", '{"card_type":"kernel_park"}',
    '{"card_type":"errand_proposal"'])
def test_anything_else_is_not_a_card(raw):
    """Every other message in this thread is markdown and must stay that
    way; a renderer that guessed would change every row."""
    from app.services.goal_thread_cards import proposal_of

    assert proposal_of(raw) is None


def test_a_park_card_is_not_rendered_here():
    """Story parks belong in the story thread where the assistant is.
    Rendering them here too would put one question in two places with
    two sets of buttons."""
    from app.services.goal_thread_cards import proposal_of

    assert proposal_of({"card_type": "kernel_park", "question_id": 1}) is None


# ── the buttons exist, and reach the endpoint ────────────────────────────

def test_the_partial_renders_both_buttons_and_calls_the_handler():
    tpl = _read("app/templates/goal_thread/_errand_proposal.html")
    assert "decideProposal({{ p.proposal_id }}, 'approve')" in tpl
    assert "decideProposal({{ p.proposal_id }}, 'decline')" in tpl
    # A decline with no reason gets re-proposed next week.
    assert "proposal-note-{{ p.proposal_id }}" in tpl


def test_the_message_row_includes_the_partial_and_only_for_a_proposal():
    row = _read("app/templates/goal_thread/_message.html")
    assert 'include "goal_thread/_errand_proposal.html"' in row
    assert "{% if msg.proposal %}" in row, (
        "the partial renders unconditionally, so every message in the "
        "thread grows an approve button")


def test_the_handler_posts_to_the_endpoint_that_exists():
    """The two halves are written in different languages in different
    files and nothing joins them. A renamed path is a 404 the person
    reads as a broken button."""
    page = _read("app/templates/goal_thread/index.html")
    assert "async function decideProposal(" in page
    assert "/kernel/errand-proposals/${id}/decide" in page

    router = _read("app/routers/kernel_errands.py")
    assert '"/goals/{pid}/kernel/errand-proposals/{proposal_id}/decide"' \
        in router


def test_the_handler_is_defined_unconditionally():
    """It landed inside `{% if auto_fire_tour %}` first, which defines it
    only on somebody's first ever visit to the thread — so the buttons
    would work once and then throw ReferenceError for ever."""
    page = _read("app/templates/goal_thread/index.html")
    at = page.index("async function decideProposal(")
    # It must sit in the MAIN script block — the one that also holds
    # `restoreGoRunState` — and NOT in `{% block extra_js %}`, whose only
    # script is wrapped in `{% if auto_fire_tour %}`.
    assert page.index("restoreGoRunState") < at
    assert at < page.index("{% block extra_js %}"), (
        "decideProposal is in extra_js, inside the first-visit tour "
        "conditional — so it exists only on somebody's first ever visit "
        "and every click after that throws ReferenceError")


def test_the_verdicts_the_page_sends_are_the_ones_the_router_accepts():
    """`decline` here and `declined` on the park cards. One character,
    and a 400 the person reads as a broken button."""
    import re

    page = _read("app/templates/goal_thread/index.html")
    handler = page[page.index("async function decideProposal("):]
    handler = handler[:handler.index("\n}")]
    sent = set(re.findall(r"verdict === '(\w+)'", handler))

    router = _read("app/routers/kernel_errands.py")
    accepted = set(re.findall(r'verdict not in \("(\w+)", "(\w+)"\)',
                              router)[0])
    assert sent <= accepted, f"the page sends {sorted(sent - accepted)}"


def test_the_uid_is_never_sent_from_the_browser():
    """Whoever approves owns every run. Ownership read off a request body
    is a claim; read off the session it is a fact."""
    page = _read("app/templates/goal_thread/index.html")
    handler = page[page.index("async function decideProposal("):]
    handler = handler[:handler.index("\n}")]
    body = handler[handler.index("JSON.stringify("):]
    body = body[:body.index("\n")]
    assert "uid" not in body and "user" not in body, body


# ── decided once ─────────────────────────────────────────────────────────

def test_deciding_stamps_the_message_so_the_buttons_do_not_return():
    """Otherwise the card renders live again for a settled proposal, the
    second click gets a 409, and a correct refusal reads as a bug."""
    router = _read("app/routers/kernel_errands.py")
    assert "UPDATE messages SET card_data = card_data ||" in router
    assert "'decided'" in router
    assert "card_data->>'card_type' = 'errand_proposal'" in router


def test_the_partial_honours_the_stamp():
    tpl = _read("app/templates/goal_thread/_errand_proposal.html")
    assert "{% if p.decided %}" in tpl
    decided = tpl[tpl.index("{% if p.decided %}"):tpl.index("{% else %}")]
    assert "decideProposal" not in decided, (
        "a decided proposal still renders buttons")


# ── it renders, for real ─────────────────────────────────────────────────

def test_jinja_actually_renders_the_row_both_ways():
    """The partial is only correct if it PARSES, and a template typo is
    invisible until somebody opens the page."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(str(ROOT / "app/templates")),
                      autoescape=select_autoescape(["html"]))
    tpl = env.get_template("goal_thread/_message.html")
    import datetime as dt

    base = {"id": "m1", "role": "agent", "sender": "go",
            "sender_name": "Goal Orchestrator", "content": "GO asks:",
            "created_at": dt.datetime(2026, 9, 12, 9, 0)}

    plain = tpl.render(msg={**base, "proposal": None})
    assert "decideProposal" not in plain, (
        "an ordinary GO message grew an approve button")

    card = tpl.render(msg={**base, "proposal": {
        "card_type": "errand_proposal", "proposal_id": 42,
        "title": "An errand that would run without you"}})
    assert "decideProposal(42, 'approve')" in card
    assert "decideProposal(42, 'decline')" in card
    assert "proposal-actions-42" in card

    done = tpl.render(msg={**base, "proposal": {
        "card_type": "errand_proposal", "proposal_id": 42,
        "decided": "approve"}})
    assert "decideProposal" not in done
    assert "Approved" in done
