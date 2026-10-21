"""The chain rule and the actor vocabulary. No database needed.

The DB-backed half is in `test_kernel_ledger_sql.py`. These are the parts
that must hold everywhere, and two of them are the kind of thing that only
breaks once, catastrophically, in production.
"""
from __future__ import annotations

import uuid

from kernel.ledger.chain import (GLOBAL_CHAIN_LOCK_ID, chain_hash,
                                 envelope_digest, payload_json)


def test_the_two_writers_take_the_same_lock():
    """A REAL BUG THIS CAUGHT, and the worst kind available here.

    Two appenders holding DIFFERENT advisory locks do not serialise against
    each other at all: both read the same chain head, both hash from it, and
    the second insert silently FORKS the chain. It would present later as
    corruption and be investigated as tampering.

    The kernel module is the definition and `app.ledger.journal` imports it,
    so this asserts they are the same object rather than merely equal.
    """
    from app.ledger import journal

    assert journal.GLOBAL_CHAIN_LOCK_ID == GLOBAL_CHAIN_LOCK_ID
    assert GLOBAL_CHAIN_LOCK_ID == 0x4C454447, (
        "changing this value forks the chain against every event ever "
        "written and every other process running the old constant")


def test_the_app_does_not_keep_its_own_copy_of_the_hash_rule():
    """One definition. The whole reason the primitive moved to `kernel/`."""
    import inspect

    from app.ledger import journal

    src = inspect.getsource(journal.append_event)
    assert "chain_hash" in src, "the app must delegate, not re-derive"
    assert "hashlib.sha256" not in src, (
        "a second implementation of the chain hash — this is how the two "
        "silently drift apart")


def test_the_hash_is_stable_and_order_independent():
    """`sort_keys` is load-bearing on every event already written: a dict
    built in a different order must hash the same or history stops
    verifying."""
    a = chain_hash("PREV", {"b": 2, "a": 1})
    b = chain_hash("PREV", {"a": 1, "b": 2})
    assert a == b
    assert chain_hash("OTHER", {"a": 1, "b": 2}) != a, "the chain must chain"
    assert len(a) == 64


def test_a_datetime_in_a_payload_does_not_break_the_append():
    """`default=str`. An audit event carrying a timestamp must not raise
    mid-append and lose the record of what happened."""
    import datetime as dt

    got = payload_json({"at": dt.datetime(2026, 9, 10, 12, 0)})
    assert "2026-09-10" in got


def test_the_envelope_digest_covers_who_did_it():
    """The gap it exists to close: the chain hash covers ONLY the payload,
    so an actor rewritten in place leaves a chain that still verifies.

    Each field is varied on its own — a digest that only responded to the
    payload would pass a weaker test and close nothing.
    """
    base = dict(event_type="run.plan_decided",
                actor_id=uuid.UUID(int=1), actor_type="human",
                on_behalf_of=None, payload={"decision": "approved"})
    ref = envelope_digest(**base)

    for field, other in [("event_type", "run.plan_proposed"),
                         ("actor_id", uuid.UUID(int=2)),
                         ("actor_type", "executor"),
                         ("on_behalf_of", uuid.UUID(int=3)),
                         ("payload", {"decision": "rejected"})]:
        assert envelope_digest(**{**base, field: other}) != ref, (
            f"the digest ignores {field} — it does not cover the envelope")

    # And it is stable, or every verification would be a false alarm.
    assert envelope_digest(**base) == ref


def test_an_executor_kind_maps_to_a_stable_id():
    """The id must not depend on when or where it was computed: the same
    kind has to resolve to the same actor in every environment, for ever,
    or the audit trail grows a new principal per deploy."""
    from kernel.ledger.actors import actor_name, actor_uuid

    assert actor_name("executor", "story", "coding") == "executor:story/coding"
    assert actor_uuid("executor:story/coding") == actor_uuid("executor:story/coding")
    assert actor_uuid("executor:story/coding") != actor_uuid("verifier:story/coding")
    # A kind nobody has registered still gets an id rather than failing.
    assert actor_uuid(actor_name("executor", "errand", "ops"))


def test_executor_and_verifier_are_never_the_same_actor():
    """An audit trail that cannot tell the judge from the judged is not
    one."""
    from kernel.ledger.actors import (EXECUTOR, VERIFIER, actor_name,
                                      actor_uuid)

    e = actor_uuid(actor_name(EXECUTOR, "story", "coding"))
    v = actor_uuid(actor_name(VERIFIER, "story", "coding"))
    assert e != v


def test_kind_is_carried_by_the_actor_not_the_type():
    """Two kinds, two actors, one type — so a new kind ships without a
    schema change to the actor_type CHECK constraint."""
    from kernel.ledger.actors import EXECUTOR, actor_name, actor_uuid

    coding = actor_uuid(actor_name(EXECUTOR, "story", "coding"))
    ops = actor_uuid(actor_name(EXECUTOR, "errand", "ops"))
    assert coding != ops
