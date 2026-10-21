"""Every card reaches Slack, renders as blocks, and its buttons work.

THREE CALL SITES IN THE WHOLE CODEBASE RELAYED A CARD. Everything else
wrote `messages.card_data` and stopped, so twenty-four of twenty-seven
kinds appeared in the panel and nowhere else -- not degraded to text,
absent. Everything the GO thread shows was in that twenty-four:
kernel parks with their approve/revise/decline, errand proposals,
verdicts, stalls, PR cards, the whole local-execution family.

Adding twenty-four calls would have fixed today and left the
twenty-fifth kind to be forgotten in exactly the same way, so the
relay hangs off `notify_new_message` -- the one call every card poster
already makes. The coverage test at the bottom is the part that keeps
it true.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest

from app.channels.slack import go_cards
from app.services.tests.slack_fixtures import (FakeSlack, fake_slack,
                                                world)


def _run(coro):
    return asyncio.run(coro)


PARK_CARD = {
    "type": "agent_card", "card_type": "kernel_park", "question_id": 42,
    "title": "One command needs your approval",
    "body": "`terraform apply` against prod",
    "needs_human": True, "allow_custom": True,
    "options": [
        {"id": "approve", "label": "✓ Approve", "value": "approve",
         "style": "primary"},
        {"id": "decline", "label": "✕ Decline", "value": "declined",
         "style": "danger"},
    ],
}

ERRAND_CARD = {
    "type": "agent_card", "card_type": "errand_proposal", "proposal_id": 7,
    "title": "An errand to run without you", "body": "Nightly reconcile",
    "options": [
        {"id": "approve", "label": "✓ Approve", "value": "approve",
         "style": "primary"},
        {"id": "decline", "label": "✕ Decline", "value": "decline",
         "style": "danger"},
    ],
}


# ── the cards render, with buttons ───────────────────────────────────────────

def test_a_kernel_park_renders_with_its_buttons():
    from app.channels.slack.blocks import render_card_as_blocks

    blocks = render_card_as_blocks(PARK_CARD)
    assert blocks, "a kernel park still falls back to plain text"
    actions = [b for b in blocks if b["type"] == "actions"]
    assert actions, "rendered with no buttons — the whole point of the card"
    ids = [e["action_id"] for e in actions[0]["elements"]]
    assert "park_approve_42" in ids


def test_an_errand_proposal_renders_with_its_buttons():
    from app.channels.slack.blocks import render_card_as_blocks

    blocks = render_card_as_blocks(ERRAND_CARD)
    ids = [e["action_id"] for b in blocks if b["type"] == "actions"
           for e in b["elements"]]
    assert "errand_approve_7" in ids and "errand_decline_7" in ids


def test_a_destructive_button_asks_first():
    """Slack has no undo and a mis-tap on a phone is one pixel away."""
    from app.channels.slack.blocks import render_card_as_blocks

    blocks = render_card_as_blocks(PARK_CARD)
    danger = [e for b in blocks if b["type"] == "actions"
              for e in b["elements"] if e.get("style") == "danger"]
    assert danger and all("confirm" in e for e in danger)


def test_an_already_decided_card_offers_no_buttons():
    from app.channels.slack.blocks import render_card_as_blocks

    blocks = render_card_as_blocks({**PARK_CARD, "decided": "answered"})
    assert not [b for b in blocks if b["type"] == "actions"], (
        "a decided card still offers buttons, so a second person presses "
        "one and gets an error they cannot act on")


def test_a_decidable_card_with_no_id_degrades_rather_than_lying():
    """Buttons that address nothing are worse than none."""
    from app.channels.slack.blocks import render_card_as_blocks

    blocks = render_card_as_blocks({**PARK_CARD, "question_id": None})
    assert not [b for b in blocks if b["type"] == "actions"]


def test_a_reporting_card_renders_as_text_not_nothing():
    from app.channels.slack.blocks import render_card_as_blocks

    blocks = render_card_as_blocks({
        "card_type": "kernel_pr", "title": "PR open",
        "body": "feat/x → main", "url": "https://example.test/pr/1"})
    assert blocks and any(b["type"] == "section" for b in blocks)


def test_long_text_is_trimmed_to_slacks_limits():
    from app.channels.slack.blocks import render_card_as_blocks

    blocks = render_card_as_blocks({
        **PARK_CARD, "body": "x" * 9000,
        "options": [{"id": "a", "label": "y" * 300, "value": "approve"}]})
    for b in blocks:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= 3000
        if b["type"] == "actions":
            for e in b["elements"]:
                assert len(e["text"]["text"]) <= 75
    assert len(blocks) <= 50


# ── and the buttons round-trip ───────────────────────────────────────────────

def test_every_action_id_we_emit_can_be_parsed_by_the_dispatcher():
    """THE CHEAPEST HIGH-VALUE TEST HERE. A renamed action_id produces a
    dead button and one INFO line: no error to the user, none to us."""
    from app.channels.slack import actions as act
    from app.channels.slack.blocks import render_card_as_blocks

    known = [go_cards.PARK_ACTION_RE, go_cards.ERRAND_ACTION_RE,
             act._ACTION_RE, act._CTRL_ACTION_RE, act._DIFF_ACTION_RE,
             act._RECOVER_ACTION_RE]
    for card in (PARK_CARD, ERRAND_CARD):
        blocks = render_card_as_blocks(card)
        for b in blocks:
            if b["type"] != "actions":
                continue
            for e in b["elements"]:
                aid = e["action_id"]
                assert any(r.match(aid) for r in known), (
                    f"{aid!r} is emitted but no dispatcher regex matches it — "
                    "that button is dead and says nothing when pressed")


@pytest.mark.parametrize("value,expected", [
    ("approve", "park_approve_42"),
    ("needs-work", "park_needs_work_42"),
    ("Revise Please", "park_revise_please_42"),
])
def test_option_values_are_made_safe_for_an_action_id(value, expected):
    blocks = go_cards.render({**PARK_CARD, "options": [
        {"id": "x", "label": "L", "value": value}]})
    ids = [e["action_id"] for b in blocks if b["type"] == "actions"
           for e in b["elements"]]
    assert expected in ids
    assert go_cards.PARK_ACTION_RE.match(expected)


# ── the fan-out: a card relays because it is a card ──────────────────────────

def test_posting_a_card_relays_it_without_anybody_asking():
    async def body():
        async with world() as (conn, pool, w):
            from app.chat_module.repository import notify_new_message

            msg_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO messages (id, thread_id, sender, role, content, "
                " token_count, card_data) "
                "VALUES ($1,$2,'executor','agent',$3,0,$4::jsonb)",
                msg_id, w["thread"], "needs you", json.dumps(PARK_CARD))

            fake = FakeSlack()
            with fake_slack(fake):
                await notify_new_message(w["thread"], msg_id)

            posts = fake.of("chat_postMessage")
            assert posts, (
                "a card was posted and never reached Slack — this is the "
                "bug: twenty-four kinds wrote card_data and stopped")
            assert posts[0].get("blocks"), (
                "it reached Slack as plain text, losing its buttons")
    _run(body())


def test_a_message_with_no_card_is_not_relayed_twice():
    """Plain messages already go through relay_all_outbound from the chat
    routers. Relaying them here as well would double every one."""
    async def body():
        async with world() as (conn, pool, w):
            from app.chat_module.repository import notify_new_message

            msg_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO messages (id, thread_id, sender, role, content, "
                " token_count) VALUES ($1,$2,'u','human','hello',0)",
                msg_id, w["thread"])
            fake = FakeSlack()
            with fake_slack(fake):
                await notify_new_message(w["thread"], msg_id)
            assert not fake.of("chat_postMessage")
    _run(body())


def test_only_one_place_relays_a_card():
    """Three sites used to. Two of them now also call notify_new_message,
    so leaving them in place would post every card twice."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    callers = []
    for path in (root / "app").rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"^\s*(?:await\s+)?(?:_slack_)?relay_card\(",
                             text, re.M):
            callers.append(f"{path.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert len(callers) == 1, (
        f"relay_card is called from {callers}. It must be called only by "
        "the fan-out, or cards post twice.")


def test_a_slack_failure_does_not_break_the_card_post():
    async def body():
        async with world() as (conn, pool, w):
            from app.chat_module.repository import notify_new_message

            msg_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO messages (id, thread_id, sender, role, content, "
                " token_count, card_data) "
                "VALUES ($1,$2,'executor','agent','x',0,$3::jsonb)",
                msg_id, w["thread"], json.dumps(PARK_CARD))
            fake = FakeSlack(fail_with={"chat_postMessage": "internal_error"})
            with fake_slack(fake):
                await notify_new_message(w["thread"], msg_id)   # must not raise
    _run(body())


# ── the coverage rule, which is what keeps this true ─────────────────────────

def test_every_card_kind_the_product_posts_has_a_renderer():
    """THE ONE THAT CATCHES THE TWENTY-FIFTH KIND.

    Walks the codebase for `"card_type": "..."` literals and fails on any
    that neither blocks.py nor go_cards.py can render. That is exactly how
    the gap accumulated: a new kind was added, nobody added a renderer,
    and `render_card_as_blocks` returned None into a debug log.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app"
    produced = set()
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # TWO SPELLINGS. Most cards say `"card_type": "x"`, but the whole
        # local-execution family says `card_data={"kind": "x"}` -- and a
        # guard that only knew the first silently covered none of them,
        # which is a guard that passes while the thing it guards is
        # broken.
        for m in re.finditer(r'"card_type":\s*"([a-z_]+)"', text):
            produced.add(m.group(1))
        for m in re.finditer(r'card_data\s*=\s*\{\s*"kind":\s*"([a-z_]+)"', text):
            produced.add(m.group(1))

    from app.channels.slack import blocks as slack_blocks
    rendered = set(re.findall(r'card_type == "([a-z_]+)"',
                              pathlib.Path(slack_blocks.__file__)
                              .read_text(encoding="utf-8")))
    rendered |= set(go_cards.REPORTING) | set(go_cards.DECIDABLE)

    assert len(produced) >= 20, (
        f"the sweep found only {len(produced)} card kinds and has gone "
        f"blind: {sorted(produced)}")
    missing = sorted(produced - rendered)
    assert not missing, (
        f"these card kinds are produced but have no Slack renderer: "
        f"{missing}. They will post as plain text and lose any buttons. "
        f"Add them to app/channels/slack/go_cards.py — REPORTING for a "
        f"card that is read, DECIDABLE for one that is answered.")


def test_a_kind_is_never_in_both_sets():
    """A decidable kind listed as reporting would render read-only and
    silently lose its buttons."""
    assert not (set(go_cards.REPORTING) & set(go_cards.DECIDABLE))
