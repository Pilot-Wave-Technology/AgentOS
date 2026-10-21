"""A card's NOTIFY must not outrun its own INSERT.

THE BUG, AND IT IS THE WHOLE OF "the approval card only shows after a
refresh". `errand_proposal_cards.sweep` wraps its insert in
`conn.transaction()`. `_post` then called `notify_new_message`, which
acquired a FRESH connection from the pool -- so `pg_notify` fired
immediately, while the row was still uncommitted. The listener woke,
ran `SELECT ... WHERE id = $1`, found nothing, and returned at its
`if not row` guard. No error, no log line, no delivery.

Measured on ERR09: the card row exists at 17:19:23 and no fan-out line
exists at all, while two ordinary messages either side of it fanned out
normally.

WHY ONLY THIS CARD. It is the only poster that uses an explicit
transaction. Park cards, verdict cards and answer cards insert in
autocommit, so their row is committed before the notify goes out and
they have always worked -- which is exactly what made this look like a
problem with the proposal card's RENDERING. Three separate attempts went
into the fan-out, the subscriber registries and the client renderer,
all of which were fine.

`_notify`'s own comment said "pg_notify is transaction-aware: it fires
on commit". True -- of the transaction's own connection, and only that.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_the_poster_notifies_where_the_row_is_visible():
    """THE PROPERTY, NOT THE MECHANISM, and the mechanism changed.

    This used to assert `conn=conn` on the notify, which was the
    original ERR09 fix: the notify rode the caller's connection so
    Postgres queued it until commit. Correct for the LISTENER, and it
    held for as long as that was the only consumer.

    Then `notify_new_message` grew a second one -- the Slack relay --
    which reads the message row back to see whether it carries a card.
    That read goes through the pool, a different connection, which
    cannot see an open transaction either. So the card reached the
    panel and never reached Slack: the same defect, one layer up, past
    the comment describing it.

    Notifying AFTER the commit satisfies both consumers at once and
    needs no special case, so that is what is pinned now. `conn=conn`
    would pass the old assertion and still be broken for Slack, which
    is exactly why this one asks a different question.
    """
    src = _read("app/services/errand_proposal_cards.py")
    sweep = src[src.index("async def sweep("):]
    tx = sweep.index("async with conn.transaction():")
    notify = sweep.index("notify_new_message")
    assert notify > tx, (
        "the notify is inside the transaction; neither the browser "
        "fan-out nor the Slack relay can see the row yet")
    assert "could not post" in sweep[:notify], (
        "the notify is not past the transaction's own except clause")
    assert "conn=conn" not in sweep[notify:notify + 120], (
        "after the commit the pooled connection is the correct one; "
        "handing the relay a connection would post to Slack from "
        "inside a transaction")


def test_notify_accepts_a_connection():
    src = _read("app/chat_module/repository.py")
    fn = src[src.index("async def notify_new_message("):]
    # To the next top-level def, whatever it happens to be.
    fn = fn[:fn.index("async def ", 10)]
    assert "conn: Optional[asyncpg.Connection] = None" in fn
    assert "if conn is not None:" in fn


def test_it_still_works_without_one():
    """Every other poster inserts in autocommit and passes nothing.
    Making the parameter required would be a wider change than the bug,
    on paths that were never broken."""
    src = _read("app/chat_module/repository.py")
    fn = src[src.index("async def notify_new_message("):]
    # To the next top-level def, whatever it happens to be.
    fn = fn[:fn.index("async def ", 10)]
    assert "pool.acquire() as own" in fn


def test_the_transactional_poster_is_the_only_one_that_needs_it():
    """If another poster grows a transaction, it needs the same
    treatment -- this records which one has it today so the next one is
    noticed."""
    src = _read("app/services/errand_proposal_cards.py")
    assert "async with conn.transaction():" in src
    for other in ("kernel_park_cards", "kernel_answer_cards"):
        assert "conn.transaction()" not in _read(f"app/services/{other}.py"), (
            f"{other} grew a transaction around its insert and must now "
            f"notify AFTER the commit, or its cards will stop arriving "
            f"live -- and stop reaching Slack -- in exactly the way the "
            f"proposal card did, twice")


def test_the_listener_still_guards_against_a_missing_row():
    """The guard is not the bug and must stay: a deleted message, or a
    replayed notify, must not raise in the listener."""
    src = _read("app/chat_module/fanout.py")
    assert "if not row:" in src
