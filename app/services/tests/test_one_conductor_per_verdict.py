"""Two conductors must not both adjudicate one claim.

MEASURED ON EXECUTION 203, claim 56:

    UniqueViolationError: duplicate key value violates unique constraint
    "verdicts_one_per_claim"
    DETAIL: Key (execution_id, claim_seq)=(203, 56) already exists.

`replicas: 2`, and `adjudicate_pass`'s in-flight supervisor is per
PROCESS -- so each conductor saw a VERIFYING execution with no verdict,
each spent a criterion container reaching the same answer, and the
second insert hit the index. The index is the backstop and it worked;
a backstop is not mutual exclusion.

The cost is not only the crash. It is TWO containers per verdict, double
the model spend on any assisted pass, and a coin toss over which of two
concurrently-computed verdicts lands.

`adjudicate_pass` had named this gap in its own docstring before the fix
existed: "there is still no claim, no SKIP LOCKED and no advisory lock
here, so two conductor processes both run the full sweep and the loser
dies on a unique index."
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from kernel.verifier import adjudicator
from kernel.worker import lease

SRC = pathlib.Path(adjudicator.__file__).read_text(encoding="utf-8")


def test_verification_has_its_own_lock_namespace():
    """Not the leg's. An execution is either legging or verifying and
    never both, so one class would work by accident -- and two keep
    pg_locks legible about WHICH pass holds an execution."""
    assert lease.VERDICT_CLASS != lease.LOCK_CLASS
    # The two-argument form of pg_try_advisory_lock takes int4.
    assert 0 < lease.VERDICT_CLASS < 2 ** 31


def test_both_paths_take_the_lock():
    """`adjudicate_pass` has a concurrent path and a serial one, and
    which is used depends on whether a supervisor was supplied -- not on
    how many conductors exist. Only locking one of them would leave the
    race open in whichever deployment took the other."""
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "adjudicate_pass")
    # Actual CALLS, not string occurrences -- the docstring and the
    # import both name it, and counting those passed vacuously.
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "verdict_lock"]
    assert len(calls) == 2, (
        "both the concurrent and the serial path must take it; "
        f"found {len(calls)}")


def test_the_loser_reports_rather_than_raising():
    """Losing the race is ordinary. Reporting it as a failure would put
    a scary action on the board every tick for a perfectly healthy
    second replica."""
    assert "adjudicating_elsewhere" in SRC
    assert "another conductor holds" in SRC


@pytest.mark.asyncio
async def test_the_lock_is_exclusive_then_released():
    """The behaviour, against a fake connection that models Postgres's
    session semantics: one holder at a time, freed on exit."""
    held: set = set()

    class _Conn:
        def __init__(self, sid):
            self.sid = sid

        async def fetchval(self, sql, cls, eid):
            assert "pg_try_advisory_lock" in sql
            if (cls, eid) in held:
                return False
            held.add((cls, eid))
            return True

        async def execute(self, sql, cls, eid):
            assert "pg_advisory_unlock" in sql
            held.discard((cls, eid))

    a, b = _Conn("a"), _Conn("b")
    async with lease.verdict_lock(a, 203) as first:
        assert first is True
        async with lease.verdict_lock(b, 203) as second:
            assert second is False, "two conductors both got the verdict"
    # released on the way out
    async with lease.verdict_lock(b, 203) as third:
        assert third is True


@pytest.mark.asyncio
async def test_a_different_execution_is_not_blocked():
    """Per execution, not global -- or one slow suite would stop every
    other story's verdict, which is the thing the concurrent path was
    built to avoid."""
    held: set = set()

    class _Conn:
        async def fetchval(self, sql, cls, eid):
            if (cls, eid) in held:
                return False
            held.add((cls, eid))
            return True

        async def execute(self, sql, cls, eid):
            held.discard((cls, eid))

    c = _Conn()
    async with lease.verdict_lock(c, 203) as one:
        async with lease.verdict_lock(c, 204) as two:
            assert one and two


@pytest.mark.asyncio
async def test_the_lock_is_released_even_when_adjudication_raises():
    """A crash inside must not leave the execution unadjudicable for the
    life of the connection."""
    held: set = set()

    class _Conn:
        async def fetchval(self, sql, cls, eid):
            held.add((cls, eid))
            return True

        async def execute(self, sql, cls, eid):
            held.discard((cls, eid))

    c = _Conn()
    with pytest.raises(RuntimeError):
        async with lease.verdict_lock(c, 203):
            raise RuntimeError("verification blew up")
    assert not held
