"""I8 — two conductor replicas must not reconcile at the same time.

`deploy/k8s/conductor.yaml` runs `replicas: 2` on the argument that
admission is `FOR UPDATE SKIP LOCKED`, leases are generation-fenced and
every transition is a CAS. `orphan_sweep` is the hole in that argument:
it reads a keep-set of container ids and then destroys the live
containers missing from it, with a substrate round trip in between. The
set is a snapshot, and the other replica can invalidate it by
dispatching -- at which point the sweep destroys a container a story is
running in and reports it as a successful reap, because destroying an
unclaimed container is exactly what it is for.

Nothing has been hurt yet only because `AGENTOS_RECONCILE_DELETE` is "0"
and the pass is a dry run. These tests are what has to be true before
that is set to "1".

WHY THE FAKES LOOK LIKE THIS. The lock space is shared between the two
fake connections, as it is in Postgres, and every fake method yields to
the event loop -- an `async def` that never awaits anything runs to
completion without giving another task a turn, so a "concurrent" test
built on instant fakes would run the two passes strictly one after the
other and pass no matter what the code did.
"""
from __future__ import annotations

import asyncio
import inspect
import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── the substrate these run against ──────────────────────────────────────

class _Locks:
    """One advisory-lock space, shared the way a database shares one."""

    def __init__(self):
        self.held: dict[tuple, object] = {}

    def take(self, owner, key) -> bool:
        if key in self.held and self.held[key] is not owner:
            return False
        self.held[key] = owner            # re-entrant per session, as in PG
        return True

    def release(self, owner, key) -> None:
        if self.held.get(key) is owner:
            del self.held[key]


class _Conn:
    """Answers the four statements `reconcile_all` issues, and no others.

    Anything else raises rather than returning None, so a query added to
    the pass later cannot be silently answered with a falsy default.
    """

    def __init__(self, locks: _Locks, *, claimed=(), goals=(),
                 releasable=()):
        self.locks = locks
        self.claimed = list(claimed)
        self.goals = list(goals)
        self.releasable = list(releasable)
        self.unlocked = 0

    async def fetchval(self, sql, *args):
        await asyncio.sleep(0)
        if "pg_try_advisory_lock" in sql:
            return self.locks.take(self, tuple(args))
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        await asyncio.sleep(0)
        if "pg_advisory_unlock" in sql:
            self.unlocked += 1
            self.locks.release(self, tuple(args))
            return "SELECT 1"
        raise AssertionError(f"unexpected execute: {sql}")

    async def fetch(self, sql, *args):
        await asyncio.sleep(0)
        if "DISTINCT" in sql:                                  # GOALS_SQL
            return list(self.goals)
        if "container_id FROM executions" in sql:              # the keep-set
            return [{"container_id": c} for c in self.claimed]
        if "UPDATE admission_slots" in sql:      # the dead-slot backstop
            # These tests are about the LOCK, so nothing is stranded:
            # the backstop finds no slot whose execution has gone.
            return []
        if "LEFT JOIN stories" in sql:           # the abandoned-run sweep
            # Likewise: no goal deleted, no story cancelled.
            return []
        if "id = ANY($1::bigint[])" in sql and "LEFT JOIN" not in sql:
            # The gone-row probe. Every held claim still has its row, so
            # the reclaim pass finds nothing to invent -- these tests are
            # about the LOCK.
            return [{"id": i} for i in args[0]]
        if "LEFT JOIN stories" in sql:                         # I6 reclaim
            # Nothing releasable. These tests are about the LOCK, and a
            # reclaim candidate here would make them about two things --
            # test_workspace_reclaim.py owns that behaviour. Named
            # explicitly rather than falling through to a falsy default,
            # which is the whole point of this fake raising.
            return list(self.releasable)
        raise AssertionError(f"unexpected fetch: {sql}")


class _Driver:
    """Records whether the sweep got as far as looking at the substrate."""

    def __init__(self, locks: _Locks, containers=(), boom=False,
                 workspaces=()):
        self.locks = locks
        self.containers = list(containers)
        self.boom = boom
        self.workspaces = list(workspaces)
        self.listed = 0
        self.held_while_listing: list[bool] = []
        self.destroyed: list[str] = []

    async def list_containers(self):
        await asyncio.sleep(0)
        self.listed += 1
        self.held_while_listing.append(bool(self.locks.held))
        if self.boom:
            raise RuntimeError("substrate API is down")
        return list(self.containers)

    async def list_workspaces(self):
        # Empty by default: the reclaim pass is driven from the
        # SUBSTRATE's claims, so an empty list is how these tests say
        # "nothing to reclaim" -- which keeps them about the lock.
        await asyncio.sleep(0)
        return list(self.workspaces)

    async def destroy(self, cid, stop_timeout=0):
        self.destroyed.append(cid)


def _container(cid, eid=None):
    return {"id": cid, "labels": ({"execution-id": str(eid)} if eid else {})}


# ── the lock is taken, and it is taken around the sweep ──────────────────

@pytest.mark.asyncio
async def test_the_pass_holds_the_lock_while_it_sweeps():
    """Held across the `list_containers` call specifically. A lock taken
    and released before the substrate round trip would leave the exact
    window the keep-set goes stale in."""
    from kernel.conductor.sweep import reconcile_all

    locks = _Locks()
    conn, driver = _Conn(locks), _Driver(locks)

    await reconcile_all(conn, driver)

    assert driver.listed == 1
    assert driver.held_while_listing == [True]
    # And given back afterwards, or the next tick on this pooled
    # connection would be the only replica that ever reconciles again.
    assert locks.held == {}
    assert conn.unlocked == 1


@pytest.mark.asyncio
async def test_the_lock_is_the_one_the_other_replica_would_contend_for():
    """Not a per-connection formality: a second connection must be
    refused while the first holds it."""
    from kernel.conductor.lock import LOCK_CLASS, LOCK_ID, is_locked

    locks = _Locks()
    a, b = _Conn(locks), _Conn(locks)

    assert await a.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)", LOCK_CLASS, LOCK_ID) is True
    assert await b.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)", LOCK_CLASS, LOCK_ID) is False
    # There is ONE reconcile lock, not one per goal: `is_locked` takes a
    # connection and nothing else. A per-goal lock would leave the
    # orphan sweep -- which is global -- unguarded again.
    assert list(inspect.signature(is_locked).parameters) == ["conn"]


# ── a second, concurrent pass skips instead of sweeping ──────────────────

@pytest.mark.asyncio
async def test_a_second_concurrent_pass_skips_and_never_reaches_the_sweep():
    """THE POINT OF THE INCREMENT. Both replicas run the pass on the same
    tick; exactly one may look at the substrate."""
    from kernel.conductor.sweep import reconcile_all

    locks = _Locks()
    a, b = _Conn(locks, claimed=["keep-me"]), _Conn(locks, claimed=["keep-me"])
    # An unclaimed, unlabelled container: whichever pass runs would reap
    # it, so "the sweep ran" is observable in `destroyed` as well.
    shared = [_container("keep-me"), _container("stray")]
    da = _Driver(locks, containers=shared)
    db = _Driver(locks, containers=shared)

    sa, sb = await asyncio.gather(
        reconcile_all(a, da, dry_run=False),
        reconcile_all(b, db, dry_run=False))

    ran = [s for s in (sa, sb) if not s["skipped"]]
    skipped = [s for s in (sa, sb) if s["skipped"]]
    assert len(ran) == 1 and len(skipped) == 1, (sa, sb)
    assert da.listed + db.listed == 1, "both replicas swept the substrate"
    assert da.destroyed + db.destroyed == ["stray"]


@pytest.mark.asyncio
async def test_the_skipped_replica_is_cheap_and_does_not_block():
    """It must not wait for the holder and must not touch the database
    beyond the one failed `pg_try_advisory_lock`."""
    from kernel.conductor.lock import LOCK_CLASS, LOCK_ID
    from kernel.conductor.sweep import reconcile_all

    locks = _Locks()
    holder, b = _Conn(locks), _Conn(locks)
    await holder.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)", LOCK_CLASS, LOCK_ID)

    # `_Conn.fetch` raises on an unrecognised statement and `_Driver` is
    # given no containers, so any work at all shows up as an exception
    # or a non-zero `listed`.
    summary = await asyncio.wait_for(reconcile_all(b, _Driver(locks)), 1.0)

    assert summary["skipped"] is True
    assert summary["goals"] == 0
    # It did not release a lock it never held -- doing so would hand the
    # holder's lock away mid-sweep.
    assert b.unlocked == 0
    assert locks.held == {(LOCK_CLASS, LOCK_ID): holder}


def test_the_lock_is_non_blocking_by_construction():
    """`pg_advisory_lock` would queue the second replica behind a sweep
    it has no reason to wait for, holding a pool connection the whole
    time. Asserted on the source because a fake cannot block."""
    src = _read("kernel/conductor/lock.py")
    assert "pg_try_advisory_lock(" in src
    assert "pg_advisory_lock(" not in src, "the blocking form crept in"


# ── released even when the pass raises ───────────────────────────────────

@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_pass_raises():
    """A sweep talks to a substrate API, so it can raise -- and
    `kernel/worker/service.py` catches that and carries on. A lock left
    held would wedge reconciliation on this pooled connection until
    something recycled it, and the symptom is silence, not an error."""
    from kernel.conductor.sweep import reconcile_all

    locks = _Locks()
    conn = _Conn(locks)

    with pytest.raises(RuntimeError):
        await reconcile_all(conn, _Driver(locks, boom=True))

    assert locks.held == {}
    assert conn.unlocked == 1

    # And the other replica can reconcile on the very next tick.
    other, driver = _Conn(locks), _Driver(locks)
    assert (await reconcile_all(other, driver))["skipped"] is False
    assert driver.listed == 1


# ── a skip is not an empty pass ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_skip_is_distinguishable_from_a_pass_that_found_nothing():
    """Both produce zero actions. Without the flag, a reconciler that had
    stopped running entirely would look exactly like a healthy cluster."""
    from kernel.conductor.lock import LOCK_CLASS, LOCK_ID
    from kernel.conductor.sweep import META_KEYS, reconcile_all

    locks = _Locks()
    quiet = await reconcile_all(_Conn(locks), _Driver(locks))

    holder, b = _Conn(locks), _Conn(locks)
    await holder.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)", LOCK_CLASS, LOCK_ID)
    skipped = await reconcile_all(b, _Driver(locks))

    # Identical on every counter: the action counts are what the caller
    # logs, and they cannot tell these two apart.
    def _counts(s):
        return {k: v for k, v in s.items() if k not in META_KEYS}

    assert _counts(quiet) == _counts(skipped) == {}
    # The flag is the whole difference.
    assert quiet["skipped"] is False and skipped["skipped"] is True


def test_the_heartbeat_does_not_count_the_flags_as_work():
    """`bool` is an `int` in Python, so a summary key that describes the
    pass lands in any naive `sum()` over it -- `dry_run: True` already
    made an idle tick heartbeat 'did 1', and `skipped` would have made
    it 2."""
    # MOVED WITH THE CODE (D2a): the tick left `service.py`, which is
    # now the process rather than the loop.
    src = _read("kernel/worker/service_tick.py")
    assert "META_KEYS" in src, (
        "service_tick.py counts the summary by hand again")
    assert 'k != "goals"' not in src


# ── it cannot collide with a leg ─────────────────────────────────────────

def test_reconcile_and_leg_locks_live_in_different_classes():
    """A reconcile that could block a leg -- or be blocked by one -- would
    be a deadlock between hygiene and the product."""
    from kernel.conductor.lock import LOCK_CLASS as RECONCILE
    from kernel.worker.lease import LOCK_CLASS as LEG

    assert RECONCILE != LEG
    # Both arguments of the two-argument form are int4; a class outside
    # that range raises at the driver rather than at review.
    assert -2 ** 31 <= RECONCILE < 2 ** 31
