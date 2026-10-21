"""A slow test suite must not stop every other story in the goal.

`adjudicate_pass` is awaited in the middle of the worker tick, and
`adjudicate_one` awaits a criterion container to completion. Serial, one
thirty-minute suite blocked admission, dispatch, leg claiming, both
drains and the credential sweep for thirty minutes -- with nothing
anywhere saying why.

The tick already made this argument about answering a question -- "an
answer is a model call taking minutes, and this pass is serial, so
answering inline would put every other story behind one person's
question". A test suite is the same sentence with a different noun.
"""

from __future__ import annotations

import asyncio
import io
import pathlib

import pytest

from kernel.verifier import adjudicator
from kernel.worker.supervisor import LegSupervisor


def _src(rel: str) -> str:
    root = pathlib.Path(__file__).resolve().parents[3]
    return io.open(root / rel, encoding="utf-8").read()


def _run(coro):
    """Run, then settle whatever is still in flight.

    Half the point of this change is that a pass RETURNS while a
    verification is still running, so these tests deliberately leave
    tasks alive. Closing the loop on them prints "Task was destroyed but
    it is pending!" -- noise that trains people to ignore test output.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.close()


class _Conn:
    """A connection that also models Postgres's advisory locks.

    `adjudicate_pass` now takes `verdict_lock` on this connection before
    spending a container -- see kernel/worker/lease.py. A fake without
    `fetchval` is no longer a fake of what this code talks to, and the
    lock is shared class-level because that is the point of it: two
    conductors, one database.
    """

    held: set = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            key = tuple(args)
            if key in _Conn.held:
                return False
            _Conn.held.add(key)
            return True
        return None

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            _Conn.held.discard(tuple(args))
        return None

    async def fetch(self, sql, *args):
        # `stranded_verifying` asks for VERIFYING runs whose pod is gone,
        # before anything is dialled. These tests are about the LOCK and
        # the concurrency, so nothing here is stranded.
        return []


@pytest.fixture(autouse=True)
def _no_locks_leak():
    """Advisory locks are per connection in Postgres and die with it;
    the fake's set is class-level, so it needs clearing between tests or
    the second test in a file finds every execution already locked."""
    _Conn.held.clear()
    yield
    _Conn.held.clear()


def _acquire():
    return _Conn()


@pytest.fixture()
def slow_verification(monkeypatch):
    """One adjudication that takes far longer than a tick."""
    started, finished = [], []

    async def _one(conn, driver, row):
        started.append(row["id"])
        await asyncio.sleep(0.25)
        finished.append(row["id"])
        return {"execution_id": row["id"], "action": "verdict"}

    monkeypatch.setattr(adjudicator, "adjudicate_one", _one)
    return started, finished


def _verifying(rows):
    async def _v(conn, goal_id=None):
        return rows
    return _v


def test_the_pass_returns_before_the_suite_finishes(monkeypatch,
                                                    slow_verification):
    started, finished = slow_verification
    monkeypatch.setattr(adjudicator, "verifying", _verifying([{"id": 1}]))
    sup = LegSupervisor()

    async def go():
        began = asyncio.get_running_loop().time()
        actions = await adjudicator.adjudicate_pass(
            _Conn(), object(), None, acquire=_acquire, supervisor=sup)
        took = asyncio.get_running_loop().time() - began
        return actions, took

    actions, took = _run(go())
    assert took < 0.15, (
        "the pass waited for the verification; a thirty-minute suite would "
        "hold the whole worker tick for thirty minutes")
    assert actions and actions[0]["action"] == "adjudicating"
    assert finished == [], "it should still be running, not done"


def test_a_running_adjudication_is_not_restarted_by_the_next_tick(
        monkeypatch, slow_verification):
    """Four seconds later the tick comes round again and the execution is
    still VERIFYING. Spawning a second one would run the suite twice."""
    started, _ = slow_verification
    monkeypatch.setattr(adjudicator, "verifying", _verifying([{"id": 7}]))
    sup = LegSupervisor()

    async def go():
        for _ in range(3):
            await adjudicator.adjudicate_pass(
                _Conn(), object(), None, acquire=_acquire, supervisor=sup)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.4)

    _run(go())
    assert started == [7], f"adjudicated {len(started)} times, expected once"


def test_two_executions_verify_at_the_same_time(monkeypatch,
                                                slow_verification):
    started, finished = slow_verification
    monkeypatch.setattr(adjudicator, "verifying",
                        _verifying([{"id": 1}, {"id": 2}, {"id": 3}]))
    sup = LegSupervisor()

    async def go():
        await adjudicator.adjudicate_pass(
            _Conn(), object(), None, acquire=_acquire, supervisor=sup)
        await asyncio.sleep(0.4)

    _run(go())
    assert sorted(finished) == [1, 2, 3], (
        "three suites did not run concurrently")


def test_serial_when_the_caller_supplies_nothing(monkeypatch,
                                                 slow_verification):
    """The console and every existing test call it with three arguments,
    and must keep the old behaviour exactly."""
    _, finished = slow_verification
    monkeypatch.setattr(adjudicator, "verifying", _verifying([{"id": 5}]))

    async def go():
        return await adjudicator.adjudicate_pass(_Conn(), object(), None)

    actions = _run(go())
    assert finished == [5], "the serial path did not wait for the verdict"
    assert actions[0]["action"] == "verdict"


def test_a_failure_in_one_does_not_stop_the_others(monkeypatch):
    seen = []

    async def _one(conn, driver, row):
        seen.append(row["id"])
        if row["id"] == 2:
            raise RuntimeError("criterion image missing")
        return {"execution_id": row["id"], "action": "verdict"}

    async def _failed(conn, row, exc):
        return {"execution_id": row["id"], "action": "adjudicator_failed"}

    monkeypatch.setattr(adjudicator, "adjudicate_one", _one)
    monkeypatch.setattr(adjudicator, "_adjudicator_failed", _failed)
    monkeypatch.setattr(adjudicator, "verifying",
                        _verifying([{"id": 1}, {"id": 2}, {"id": 3}]))
    sup = LegSupervisor()

    async def go():
        await adjudicator.adjudicate_pass(
            _Conn(), object(), None, acquire=_acquire, supervisor=sup)
        await asyncio.sleep(0.15)

    _run(go())
    assert sorted(seen) == [1, 2, 3], (
        "one execution's failure stopped the others; they are unrelated "
        "runs that have done nothing wrong")


def test_the_cross_process_race_is_closed():
    """THIS TEST USED TO ASSERT THE OPPOSITE, and was right to.

    It required the gap to stay written down -- "there is still no
    claim, no SKIP LOCKED and no advisory lock here, so two conductor
    processes both run the full sweep" -- so that "Conductor x2, safe to
    run N" could not read as true of this pass.

    It became true on execution 203, claim 56, when both replicas
    adjudicated at once and the loser died on `verdicts_one_per_claim`.
    `verdict_lock` closes it, and this now guards the fix rather than
    the admission.
    """
    src = _src("kernel/verifier/adjudicator.py")
    assert "verdict_lock" in src
    assert "WHAT THIS DOES NOT FIX" not in src


def test_a_second_conductor_does_not_spend_a_container(monkeypatch,
                                                       slow_verification):
    """The measured cost was not only the crash: both replicas ran the
    full criterion set before one of them lost the insert."""
    started, _finished = slow_verification
    monkeypatch.setattr(adjudicator, "verifying", _verifying([{"id": 7}]))

    async def go():
        # Two PROCESSES: a supervisor each (the in-flight set is
        # per-process and cannot see the other), one database.
        first = await adjudicator.adjudicate_pass(
            _Conn(), object(), None, acquire=_acquire,
            supervisor=LegSupervisor())
        await asyncio.sleep(0.05)
        second = await adjudicator.adjudicate_pass(
            _Conn(), object(), None, acquire=_acquire,
            supervisor=LegSupervisor())
        await asyncio.sleep(0.35)
        return first, second

    _first, second = _run(go())
    assert started.count(7) == 1, (
        f"the criteria ran {started.count(7)} times across two conductors")
    # The SWEEP still says "adjudicating" for both, and that is correct:
    # the lock is taken inside the spawned task, because an advisory lock
    # must be held on the connection doing the work and the sweep's is
    # not that one. The guarantee is the count above -- one container per
    # verdict -- not the word the sweep used before it knew.
    assert second and second[0]["action"] == "adjudicating"
