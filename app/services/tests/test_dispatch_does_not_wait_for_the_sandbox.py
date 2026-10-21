"""Creating a sandbox and waiting for one are different jobs.

WHAT WAS TRUE BEFORE. `dispatch_admitted` did six things inline: CAS the
state to DISPATCHING, `driver.create()`, `driver.await_ready()`, read the
generation, INSERT the lease, CAS to RUNNING. Five of them are database
statements measured in milliseconds. The third is a poll loop with a
budget of thirty seconds on Docker and two minutes on Kubernetes, and a
real sandbox took about 27 seconds of it — the bridge shim has to bind
its port, and on a pod two init containers run first, one of them a git
clone.

THE MEASUREMENT. Dispatch is called from a serial `for` loop in two
places (`conductor_tick`, and `dispatch_pass` limited to four). Four
sandboxes therefore cost about 108 seconds, and for that entire time the
pass advanced NOTHING else: no leg, no park relay, no credential sweep,
no reconcile, no admission. Ramping twenty stories from cold was about
nine minutes of a conductor that looked wedged and was merely waiting.

WHY THE FIX IS SHAPED THIS WAY. Readiness never needed the caller to
hold still for it — the sandbox is either answering or it is not, and
finding out costs one round trip. So dispatch now stops after recording
the container id, with the row still DISPATCHING, and a cheap pass
promotes whatever is answering over the DISPATCHING -> RUNNING edge the
state machine has declared since v1. Three properties have to survive
the split, and they are what most of this file tests:

  a sandbox created and never promoted must still be reclaimable — the
  old code destroyed it when `await_ready` raised, and nothing else in
  the system has a deadline for a DISPATCHING row that HAS a container
  (`reconcile.py` calls that "healthy", for ever)

  the lease must be written exactly once, against the generation that
  is current when the run actually starts — not one read seconds
  earlier, before a reconcile that may have fenced it

  two replicas racing must still resolve to one winner, which is what
  the CAS has always been for
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from kernel.conductor import promote
from kernel.conductor.dispatch import dispatch_admitted
from kernel.conductor.dispatch_limit import (DEFAULT_DISPATCH_CONCURRENCY,
                                             DISPATCH_CONCURRENCY_ENV,
                                             dispatch_concurrency)
from kernel.worker.concurrency import LEG_CONCURRENCY_ENV, leg_concurrency

SPEC = SimpleNamespace(env={"BRIDGE_PORT": "9201"})


def _answer(value):
    """A query that returns `value` whatever it is asked."""
    async def _query(sql, *args):
        return value
    return _query


# ── the doubles ──────────────────────────────────────────────────────

class _Db:
    """One in-memory kernel schema, shared by as many connections as a
    test needs — which is how two racing promoters are modelled."""

    def __init__(self, state="ADMITTED", generation=3, waited=1.0):
        self.row = {"id": 42, "state": state, "container_id": None,
                    "generation": generation, "waited": waited,
                    "kind": "story", "domain": "coding", "goal_id": "g",
                    "stall_reason": None}
        self.leases: list[dict] = []

    def live_leases(self):
        return [l for l in self.leases if l["released_at"] is None]


class _Conn:
    """Answers the statements dispatch and promote actually make.

    Each rule is one statement. A CAS is applied without an intervening
    `await`, which is how a Postgres row lock behaves from the point of
    view of two coroutines racing for it — a fake that awaited in the
    middle would let both through and prove nothing.
    """

    def __init__(self, db: _Db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield self

    def _cas(self, allowed: bool, **sets) -> str:
        """A guarded UPDATE, applied with no `await` in the middle."""
        if not allowed:
            return "UPDATE 0"
        self.db.row.update(sets)
        return "UPDATE 1"

    async def execute(self, sql, *args):
        row = self.db.row
        if "SET state = 'DISPATCHING'" in sql:
            return self._cas(row["state"] == "ADMITTED", state="DISPATCHING")
        if "SET container_id = $1" in sql:
            return self._cas(row["state"] == "DISPATCHING"
                             and not row["container_id"],
                             container_id=args[0])
        if "SET container_id = NULL" in sql:
            return self._cas(row["state"] == "DISPATCHING"
                             and row["container_id"] == args[2],
                             container_id=None, stall_reason=args[1])
        if "INSERT INTO execution_leases" in sql:
            assert not self.db.live_leases(), "the unique index would refuse"
            self.db.leases.append(dict(
                zip(("execution_id", "holder", "generation"), args),
                released_at=None))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {sql}")

    async def fetchval(self, sql, *args):
        assert "SET state = 'RUNNING'" in sql, sql
        row = self.db.row
        taken = self._cas(row["state"] == "DISPATCHING"
                          and row["container_id"] == args[1], state="RUNNING")
        return row["generation"] if taken == "UPDATE 1" else None

    async def fetchrow(self, sql, *args):
        row = self.db.row
        if "SELECT state, container_id" in sql:
            return {"state": row["state"], "container_id": row["container_id"]}
        assert "SELECT id, container_id" in sql, sql
        return dict(row) if self._dispatched() else None

    async def fetch(self, sql, *args):
        assert "state = 'DISPATCHING'" in sql, sql
        return [dict(self.db.row)] if self._dispatched() else []

    def _dispatched(self) -> bool:
        return (self.db.row["state"] == "DISPATCHING"
                and bool(self.db.row["container_id"]))


class _Driver:
    """A substrate that answers, does not answer, or died."""

    def __init__(self, answer="waiting", create_error=None):
        self.answer, self.create_error = answer, create_error
        self.live: set[str] = set()
        self.destroyed: list[str] = []
        self.ready_calls: list[tuple] = []

    async def create(self, spec):
        if self.create_error is not None:
            raise self.create_error
        cid = f"sandbox-{len(self.live) + len(self.destroyed) + 1}"
        self.live.add(cid)
        return cid

    async def resolve_bridge_port(self, container_id):
        return 9201

    async def await_ready(self, container_id, timeout=None, bridge_port=None):
        self.ready_calls.append((container_id, timeout, bridge_port))
        if self.answer == "ready":
            return True
        if self.answer == "gone":
            raise RuntimeError(f"{container_id} exited before becoming ready")
        raise TimeoutError(f"{container_id} is not answering yet")

    async def destroy(self, container_id, stop_timeout=10):
        self.destroyed.append(container_id)
        self.live.discard(container_id)


# ── dispatch stops at the sandbox ────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_never_waits_for_the_sandbox_to_answer():
    db, driver = _Db(), _Driver()
    await dispatch_admitted(_Conn(db), driver, 42, SPEC)
    assert driver.ready_calls == [], (
        "dispatch waited for readiness; that wait is the 27 seconds the "
        "whole pass used to spend per sandbox")


@pytest.mark.asyncio
async def test_the_row_holds_its_sandbox_the_moment_dispatch_returns():
    # RECLAIMABILITY DEPENDS ON THIS. While the container id was written
    # only at the RUNNING step, a live container existed for the length
    # of the startup that no row named: a cancel could not reap it and
    # reconcile read the row as "DISPATCHING with no compute".
    db, driver = _Db(), _Driver()
    cid = await dispatch_admitted(_Conn(db), driver, 42, SPEC)
    assert (db.row["state"], db.row["container_id"]) == ("DISPATCHING", cid)


# ── the promote pass ─────────────────────────────────────────────────

async def _dispatched(answer="waiting", waited=1.0):
    db, driver = _Db(waited=waited), _Driver(answer=answer)
    conn = _Conn(db)
    await dispatch_admitted(conn, driver, 42, SPEC)
    return db, driver, conn


@pytest.mark.asyncio
async def test_a_sandbox_that_is_answering_reaches_running_with_a_lease():
    db, driver, conn = await _dispatched("ready")
    actions = await promote.promote_ready(conn, driver)
    assert [a["action"] for a in actions] == ["promoted"]
    assert db.row["state"] == "RUNNING"
    assert [(l["holder"], l["generation"]) for l in db.leases] == [
        (db.row["container_id"], 3)]


@pytest.mark.asyncio
async def test_a_sandbox_that_is_not_answering_yet_is_left_exactly_alone():
    db, driver, conn = await _dispatched("waiting")
    actions = await promote.promote_ready(conn, driver)
    assert [a["action"] for a in actions] == ["waiting"]
    assert db.row["state"] == "DISPATCHING" and db.leases == []
    assert driver.destroyed == [], "a slow start is not a failed one"


@pytest.mark.asyncio
async def test_the_lease_is_written_once_however_often_the_pass_runs():
    # The pass is level-triggered and runs every tick. Writing the lease
    # twice would hit the one-live-lease unique index and, worse, would
    # mean the CAS was not what gates the promotion.
    db, driver, conn = await _dispatched("ready")
    first = await promote.promote_ready(conn, driver)
    second = await promote.promote_ready(conn, driver)
    assert [a["action"] for a in first] == ["promoted"]
    assert second == [], "a RUNNING row is not a DISPATCHING one"
    assert len(db.leases) == 1


@pytest.mark.asyncio
async def test_two_promoters_racing_produce_one_lease_and_one_sandbox():
    # Two conductor replicas, one row. The CAS on
    # `state = 'DISPATCHING' AND container_id = $2` is the arbiter; the
    # loser must also not destroy the sandbox the winner is now using.
    db, driver, conn = await _dispatched("ready")
    outcomes = await asyncio.gather(
        promote.promote_ready(_Conn(db), driver),
        promote.promote_ready(_Conn(db), driver))
    verdicts = [a["action"] for pass_ in outcomes for a in pass_]
    assert verdicts.count("promoted") == 1, verdicts
    assert len(db.leases) == 1
    assert driver.destroyed == []
    assert db.row["container_id"] in driver.live


@pytest.mark.asyncio
async def test_the_generation_fenced_on_is_the_one_at_promotion():
    # THE QUESTION THE SPLIT RAISES. The generation used to be read
    # milliseconds before the lease, inside the same call as the
    # container create. Now seconds pass, and reconcile advances the
    # generation when it recovers a run — so the lease must carry what
    # is true when the run actually starts, or the worker would be
    # fenced by a number that moved while its pod was booting.
    db, driver, conn = await _dispatched("ready")
    db.row["generation"] = 9                       # a recovery, mid-startup
    await promote.promote_ready(conn, driver)
    assert [l["generation"] for l in db.leases] == [9]


# ── a sandbox that never becomes usable ──────────────────────────────

@pytest.mark.asyncio
async def test_a_sandbox_that_died_is_not_waited_out():
    # Both drivers raise RuntimeError with the container's last words
    # for "it exited before becoming ready". That is a verdict, not a
    # delay, and the old code acted on it immediately.
    db, driver, conn = await _dispatched("gone")
    cid = db.row["container_id"]
    actions = await promote.promote_ready(conn, driver)
    assert [a["action"] for a in actions] == ["reclaimed"]
    assert driver.destroyed == [cid] and cid not in driver.live


@pytest.mark.asyncio
async def test_a_sandbox_that_never_answers_is_reclaimed_not_stranded():
    # The deadline `await_ready` used to supply. Nothing else has one:
    # reconcile recovers a DISPATCHING row whose compute is GONE and
    # reports a DISPATCHING row that still HAS a container as healthy.
    db, driver, conn = await _dispatched("waiting",
                                         waited=promote.STARTUP_BUDGET_S + 1)
    cid = db.row["container_id"]
    actions = await promote.promote_ready(conn, driver)
    assert [a["action"] for a in actions] == ["reclaimed"]
    assert driver.destroyed == [cid]
    # LEFT DISPATCHING WITH NO CONTAINER, which is exactly the state
    # dispatch's own failure path leaves behind and the one reconcile
    # recovers — ADMITTED, generation advanced, ready to be built again.
    assert (db.row["state"], db.row["container_id"]) == ("DISPATCHING", None)
    assert "sandbox" in db.row["stall_reason"]


@pytest.mark.asyncio
async def test_a_failed_create_leaves_a_recoverable_row_and_no_container():
    db = _Db()
    driver = _Driver(create_error=OSError("no substrate today"))
    with pytest.raises(OSError):
        await dispatch_admitted(_Conn(db), driver, 42, SPEC)
    assert (db.row["state"], db.row["container_id"]) == ("DISPATCHING", None)
    assert driver.live == set() and db.leases == []


@pytest.mark.asyncio
async def test_a_sandbox_nobody_can_claim_is_destroyed_rather_than_leaked():
    # The row moved out from under the create — reconcile recovering it,
    # or another replica. Recording the id would overwrite a claim that
    # is not ours; returning quietly would leave a container running
    # that no row names and the orphan sweep skips.
    db, driver = _Db(), _Driver()
    conn = _Conn(db)

    original = conn.execute

    async def _steal(sql, *args):
        if "SET container_id = $1" in sql:
            db.row["state"] = "ADMITTED"       # reconcile got there first
        return await original(sql, *args)

    conn.execute = _steal
    with pytest.raises(RuntimeError):
        await dispatch_admitted(conn, driver, 42, SPEC)
    assert driver.destroyed and driver.live == set()


# ── the two numbers are not one number ───────────────────────────────

def test_the_dispatch_limit_is_not_the_leg_limit(monkeypatch):
    # A leg holds a database connection for minutes, so its ceiling is
    # the pool. Dispatch holds nothing and costs an API call per row.
    # Moving one must not move the other, in either direction.
    monkeypatch.delenv(DISPATCH_CONCURRENCY_ENV, raising=False)
    monkeypatch.setenv(LEG_CONCURRENCY_ENV, "3")
    assert leg_concurrency() == 3
    assert dispatch_concurrency() == DEFAULT_DISPATCH_CONCURRENCY == 24
    monkeypatch.setenv(DISPATCH_CONCURRENCY_ENV, "40")
    assert (dispatch_concurrency(), leg_concurrency()) == (40, 3)
    # And a typo falls back rather than halting dispatch silently and
    # for ever, which is what 0 or a negative number would do.
    for nonsense in ("nine", "0", "-4", ""):
        monkeypatch.setenv(DISPATCH_CONCURRENCY_ENV, nonsense)
        assert dispatch_concurrency() == DEFAULT_DISPATCH_CONCURRENCY


@pytest.mark.asyncio
async def test_the_pass_asks_every_sandbox_at_once_not_one_after_another():
    # The property that makes the promote pass cheap: N unready
    # sandboxes cost ONE probe timeout, not N of them. Asserted on
    # behaviour — the probes must all be in flight before any returns.
    db, driver, conn = await _dispatched("waiting")
    seen, released = [], asyncio.Event()

    async def _slow(container_id, timeout=None, bridge_port=None):
        seen.append(container_id)
        await released.wait()
        raise TimeoutError("still starting")

    driver.await_ready = _slow
    conn.fetch = _answer([dict(db.row, id=i, container_id=f"sandbox-{i}")
                          for i in range(1, 4)])
    task = asyncio.create_task(promote.promote_ready(conn, driver))
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(seen) == 3, f"probes are serialised: {seen}"
    released.set()
    await task
