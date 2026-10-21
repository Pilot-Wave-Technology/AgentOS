"""The same trap, walked into twice, in the same place.

THE FIRST TIME. `errand_proposal_cards` is the one card poster that
wraps its insert in a transaction. It called `notify_new_message` on a
POOLED connection, so the notify fired while the row was still
uncommitted; the listener looked the message up, found nothing, and
returned at its `if not row` guard. No error, no log line. The symptom
was "the errand approval card only appears after a refresh", and it
lasted weeks (ERR09). The fix was to pass `conn` so Postgres queues the
notify until commit.

THE SECOND TIME, WHICH IS WHAT THIS FILE IS FOR. `notify_new_message`
later grew a second job: relaying the card to Slack. That relay reads
the message row back to see whether there is a card on it -- through
`get_pool()`, a DIFFERENT connection, which cannot see the caller's
open transaction either. So the card reached the panel and never
reached Slack, silently, for the identical reason, in the identical
place, past a comment explaining the identical trap.

Observed on CALY5: the errand proposal was on the GO thread in the UI
and absent from its channel.

TWO FIXES, BECAUSE ONE OF THEM IS ONLY FOR THIS CALLER. The poster now
notifies AFTER its commit, which is simplest and needs no special
case. And the relay takes the caller's connection when it is given
one, so the next poster to use a transaction does not walk into it a
third time.
"""
from __future__ import annotations

import inspect

from app.chat_module import repository
from app.services import errand_proposal_cards as cards


# ── the poster notifies after it commits ─────────────────────────────

def test_the_notify_is_outside_the_transaction():
    """Inside it, neither the browser fan-out nor the Slack relay can
    see the row they are about to look up."""
    src = inspect.getsource(cards.sweep)
    tx = src.index("async with conn.transaction():")
    notify = src.index("notify_new_message")
    assert notify > tx, (
        "the notify is inside the transaction again; the card will "
        "reach the panel and not Slack")

    # And genuinely outside the block, not merely later in the file.
    # Indentation is the wrong test — the call sits in its own `try`
    # inside the loop, so it is MORE indented than the transaction line
    # while still being outside it. The `except` that closes the
    # transaction's own try is the real boundary.
    closes_tx = src.index('could not post')
    assert notify > closes_tx, (
        "the notify is still nested inside the transaction block")


def test_the_poster_no_longer_passes_its_own_connection():
    """Passing `conn` was the ERR09 fix and is now the wrong shape:
    after the commit a pooled notify is correct, and a connection
    handed to the relay would post Slack inside the transaction."""
    src = inspect.getsource(cards)
    i = src.index("await notify_new_message")
    assert "conn=conn" not in src[i:i + 120]


def test_post_returns_the_message_id_so_sweep_can_notify():
    """It returned a bool, which told `sweep` that something was
    posted but not WHAT, so the notify could not be moved out."""
    src = inspect.getsource(cards._post)
    assert "return msg_id" in src
    assert inspect.signature(cards._post).return_annotation == "str"


def test_a_failed_post_does_not_notify():
    """A rolled-back insert has no row. Notifying for it would send the
    listener and the relay after a message that does not exist."""
    src = inspect.getsource(cards.sweep)
    assert "continue" in src, (
        "the exception path falls through to the notify")
    # The guard is on the id, so a goal with no thread (which returns
    # "") is skipped too.
    assert 'if msg_id:' in src


# ── and the relay defends itself for the next caller ─────────────────

def test_the_relay_uses_the_callers_connection_when_given_one():
    src = inspect.getsource(repository._relay_card_if_any)
    assert "conn or get_pool()" in src, (
        "the relay always reads through the pool, so a card written "
        "inside a transaction is invisible to it")


def test_the_notify_passes_its_connection_through_to_the_relay():
    """The parameter existing is not the point; reaching the relay is."""
    src = inspect.getsource(repository.notify_new_message)
    assert "_relay_card_if_any(thread_id, message_id, conn)" in src, (
        "notify_new_message takes a connection and does not give it to "
        "the relay, which is the whole defect")


def test_the_relay_signature_accepts_either():
    params = inspect.signature(repository._relay_card_if_any).parameters
    assert "conn" in params
    assert params["conn"].default is None, (
        "every existing caller passes two arguments; a required third "
        "would break them all")


def test_relay_card_for_message_takes_whatever_can_fetch():
    """It is typed as a pool but is handed a connection now. asyncpg's
    Connection and Pool both expose `fetchrow`/`fetchval`, which is the
    whole of what it uses — so this pins that it uses nothing else."""
    src = inspect.getsource(
        __import__("app.services.card_fanout", fromlist=["x"])
        .relay_card_for_message)
    for method in ("acquire(", "release(", "transaction("):
        assert method not in src, (
            f"relay_card_for_message calls pool-only {method}, so it "
            f"cannot be handed a plain connection")
