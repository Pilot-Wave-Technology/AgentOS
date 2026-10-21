"""A plan card went missing because a VERDICT claim raised.

WHAT HAPPENED. `sweep()` announces three kinds of thing in one pass, in
order: stalls, verdicts, then questions -- and a plan card is a question
card. Every loop claimed its row with a conditional UPDATE placed
OUTSIDE its try block; only the post was guarded.

`kernel.verdicts` is append-only. Its trigger exempts the table owner,
and the control panel connects as `agentos`, which holds UPDATE -- so the
privilege check passed and the TRIGGER refused, with restrict_violation.
That exception left `sweep` before the question loop ran.

The cost: ten verdicts waiting, the oldest from 2026-09-17, and no plan
or question card in any thread for two and a half days. The sweep logged
"kernel park cards: sweep failed" every five seconds throughout. Nobody
read it. It was found when a person asked why one story's plan card had
not appeared -- a symptom two days downstream of its cause, in a
different kind of card, on a story that had nothing to do with verdicts.

WHAT THIS PINS, and it is the structural half rather than the trigger
half (migrations/verdicts_allow_the_card_claim.sql is the other): a loop
that announces several kinds of thing must not let one kind decide
whether the others are announced at all. Per-row isolation alone is not
enough -- `conn.fetch` for a kind can fail before any row exists -- so
the guard is per kind as well.
"""
from __future__ import annotations

import inspect
import re

from app.services import kernel_park_cards

SOURCE = inspect.getsource(kernel_park_cards.sweep)


def _loop_bodies() -> dict:
    """The three card kinds' loops, split out of `sweep`'s source.

    Read from source rather than executed because `sweep` wants a live
    pool, three tables and a chat module. The property being pinned is
    structural -- where the claim sits relative to the guard -- and
    structure is legible in the text.
    """
    out, current = {}, None
    for line in SOURCE.splitlines():
        marker = re.search(r'with _kind\("(\w+)"\)', line)
        if marker:
            current = marker.group(1)
            out[current] = []
        elif current:
            out[current].append(line)
    return out


def test_every_card_kind_is_swept_inside_its_own_guard():
    """One kind raising must not end the pass for the kinds after it."""
    kinds = set(_loop_bodies())
    assert kinds == {"stall", "verdict", "question"}, (
        f"expected each card kind inside its own `with _kind(...)` guard; "
        f"found {sorted(kinds)}. A kind swept outside one can end the "
        f"whole sweep, and the kinds after it are never announced.")


def test_the_claim_is_inside_the_guard_not_before_it():
    """The exact shape of the defect.

    A claim outside the try is an unguarded database write in a loop
    whose whole purpose is to keep going. It was outside for all three
    kinds; the verdict one is merely the first that started failing.
    """
    for kind, body in _loop_bodies().items():
        text = "\n".join(body)
        claim = next((i for i, ln in enumerate(body)
                      if "card_posted_at = now()" in ln
                      or "SET stall_card_note" in ln), None)
        assert claim is not None, f"no claim found in the {kind} loop"
        guard = next((i for i, ln in enumerate(body)
                      if ln.strip() == "try:"), None)
        assert guard is not None, f"the {kind} loop has no try block"
        assert guard < claim, (
            f"the {kind} loop claims its row on line {claim} but does not "
            f"open its try until line {guard}. A claim that raises then "
            f"escapes the loop, and on the verdict kind that silenced "
            f"every question and plan card for two and a half days.")
        assert "except Exception" in text, (
            f"the {kind} loop has no except; a guard that does not catch "
            f"is a guard in name")


def test_questions_are_swept_last_and_that_is_why_this_matters():
    """Ordering is load-bearing, so it is written down rather than
    discovered again.

    Questions -- and therefore plan cards -- are announced after stalls
    and verdicts. That ordering is fine in itself, but it means any
    unguarded failure earlier in the pass is invisible in exactly the
    place a person looks: the assistant thread, waiting for a plan.
    """
    order = list(_loop_bodies())
    assert order[-1] == "question", (
        f"question cards are no longer swept last (order: {order}). If "
        f"they move, this test's premise changes -- but so does which "
        f"kind gets silenced first when something upstream breaks.")


def test_the_migration_that_unblocks_the_verdict_claim_is_registered():
    """The trigger half. Narrowing it is what makes the claim legal.

    Without the migration the claim raises for ever and the guard above
    merely contains the damage -- verdict cards would never post, and the
    log would fill with a caught exception instead of an uncaught one.
    """
    import pathlib

    from app.services.tests.shipped_tree import IS_DISTRIBUTION, baseline_mentions

    if IS_DISTRIBUTION:
        # The narrowed trigger function itself, as the schema ships it.
        assert baseline_mentions(
            r"FUNCTION kernel\.verdicts_are_append_only\(\).*?"
            r"OLD\.card_posted_at IS NULL.*?"
            r"to_jsonb\(NEW\) - 'card_posted_at'.*?"
            r"= to_jsonb\(OLD\) - 'card_posted_at'.*?\$\$;"), (
            "the narrowed trigger should permit only a card claim, compared "
            "with the rest of the row held equal")
        return
    root = pathlib.Path(__file__).resolve().parents[3]
    name = "verdicts_allow_the_card_claim"
    sql = root / "migrations" / f"{name}.sql"
    assert sql.exists(), f"{name}.sql is missing"
    body = sql.read_text(encoding="utf-8")
    assert "card_posted_at" in body and "to_jsonb" in body, (
        "the narrowed trigger should permit only a card claim, compared "
        "with the rest of the row held equal")
    sequence = (root / "migrations" / "SEQUENCE").read_text(encoding="utf-8")
    assert name in sequence.split(), (
        f"{name} is not in migrations/SEQUENCE, so it will never run")
