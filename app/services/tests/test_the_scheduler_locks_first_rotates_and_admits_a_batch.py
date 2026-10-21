"""Three scheduling defects, and the one that destroyed live pods.

C5 — THE REPLICA THAT WAS NOT RUNNING THE LEG COULD DELETE THE WORKSPACE.

WHAT WAS TRUE BEFORE. `worker/loop.py::_advance_one` ran three steps in
this order: `ensure_reachable` (probe, and on Kubernetes destroy),
`ensure_setup` (the tree's own `pip install` / `npm ci`), and only then
`run_one_leg` — which is where `leg_lock` was taken. So the mutual
exclusion the whole design leans on began AFTER the two most destructive
steps had already run. The conductor runs two replicas and `claimable`
hands the same RUNNING row to both.

THE MEASURED EVIDENCE. `bridge_health` is probed with `timeout=3`. The
shim served one request at a time and `env_setup` held it for the whole
install (E14 made it a job). Three seconds into the holder's install, the
NON-holding replica concluded the sandbox was dead and, because
SANDBOX_MODEL is "pod", destroyed the pod underneath the running leg:

    execution 192: bridge dead; destroying the pod so dispatch rebuilds it

NCAL4 runs 191 and 192, both at the first step after plan approval. From
outside: "the workspace disappears every time."

WHY THE FIX IS THE ORDER AND NOT THE TIMEOUT. A longer probe moves the
race; it does not close it. Two replicas acting on one sandbox is the
bug, and the lock is the thing that already knows which one owns it. So
the lock is taken FIRST, and env setup moved inside it too — it WRITES
(sentinel, SETUP_FAILED.md, stall_reason), its once-per-sandbox marker is
check-then-act, and it is the very 120s window in which the bridge cannot
answer. These tests assert the ORDER and the ABSENCE OF THE DESTROY, not
any particular spelling of it.

C3 — HARD STARVATION. `claimable` ended `ORDER BY e.id` with no offset
and no rotation, and the caller took `rows[:max_concurrent]`. The lowest
id re-won the slot it had just freed on every pass, for ever: with more
runnable executions than slots, the high-id stories never ran at all —
not slowly, never. The rotation test below is the one that would have
caught it, and it is written as three runnable rows, one slot, three
passes.

C4 — UNBOUNDED ADMISSION. `admit_pending`'s `FOR UPDATE SKIP LOCKED`
select had no LIMIT. Bucket ceilings bound one (goal, domain, kind) and
nothing bounded the pass, so a burst admitted ~40 executions and
provisioned ~40 pods, ~36 of them idle, each holding a PVC.

Fakes throughout, and no database: the defects are decisions the
scheduler makes, and the reason all three survived review is that none of
them could be asked a question without a live Postgres.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from kernel.conductor import admit as admit_mod
from kernel.conductor.admission_limit import (DEFAULT_ADMIT_BATCH,
                                              MAX_ADMIT_BATCH, admit_batch)
from kernel.worker import advance, fairness
from kernel.worker import loop as worker_loop

KEY = fairness.LAST_ADVANCED
NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.timezone.utc)


def _run(coro):
    return asyncio.run(coro)


# ── C5 · nobody acts on a sandbox they do not hold the lock for ──────────

class _Conn:
    """Just enough asyncpg to answer what this path asks, and a log."""

    def __init__(self, log, granted=True):
        self.log, self.granted, self.executed = log, granted, []

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            self.log.append("lock")
            return self.granted
        return None

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            self.log.append("unlock")
            return "SELECT 1"
        self.executed.append(sql)
        return "UPDATE 1"


class _Driver:
    """A pod-model driver: the substrate where "rebuild" means "destroy"."""

    SANDBOX_MODEL = "pod"

    def __init__(self, log):
        self.log, self.destroyed = log, []

    async def resolve_bridge_port(self, container_id):
        self.log.append("resolve_port")
        return 9931

    async def resolve_bridge_host(self, container_id):
        self.log.append("resolve_host")
        return "10.4.0.7"

    async def destroy(self, container_id, stop_timeout=0):
        self.log.append("destroy")
        self.destroyed.append(container_id)


def _row():
    return {"id": 192, "kind": "story", "domain": "code", "generation": 0,
            "container_id": "pod-192", "workspace_path": "/workspace/192",
            "task": "", "terminal_command": None, "workspace_source": None}


def _wire(monkeypatch, log, *, healthy, leg=None, setup=None):
    """Patch the three seams the advance path crosses, each logging."""
    async def _health(port=None, host=None, timeout=None):
        log.append("probe")
        return healthy

    async def _setup(conn, row):
        log.append("setup")
        return setup

    async def _leg(conn, driver, row, run_leg_fn, make_executor, adapter):
        log.append("leg")
        if leg is not None:
            raise leg
        return {"execution_id": row["id"], "action": "next_leg"}

    monkeypatch.setattr("kernel.bridge.client.bridge_health", _health)
    monkeypatch.setattr(advance, "ensure_setup", _setup)
    monkeypatch.setattr(worker_loop, "leg_body", _leg)


def test_a_replica_without_the_leg_lock_never_destroys_the_pod(monkeypatch):
    """THE INCIDENT, as an assertion. The non-holder is told "busy" before
    it has resolved a port, probed a bridge, or written a row — so there
    is no moment at which it could form the opinion that killed pod 192."""
    log = []
    _wire(monkeypatch, log, healthy=False)
    conn, driver = _Conn(log, granted=False), _Driver(log)

    got = _run(advance.advance_one(conn, driver, _row(), None, None, None))

    assert [a["action"] for a in got] == ["busy"]
    assert driver.destroyed == [], "a replica that lost the lock killed a pod"
    assert log == ["lock"], f"it did more than ask for the lock: {log}"
    assert conn.executed == [], "it wrote to the database without the lock"


def test_the_lock_is_held_before_anything_probes_or_installs(monkeypatch):
    """The order is the fix. Probe and setup are both inside the lock, and
    setup is inside it because it WRITES and because it is the 120-second
    window in which the bridge cannot answer a probe."""
    log = []
    _wire(monkeypatch, log, healthy=True)

    got = _run(advance.advance_one(_Conn(log), _Driver(log), _row(),
                                   None, None, None))

    assert [a["action"] for a in got] == ["next_leg"]
    for later in ("probe", "setup", "leg"):
        assert log.index("lock") < log.index(later), f"{later} beat the lock"
    assert log.index("setup") < log.index("leg"), "setup ran after the leg"
    assert log[-1] == "unlock", f"the lock outlived the leg: {log}"


def test_a_leg_does_not_start_while_the_install_is_still_running(
        monkeypatch):
    """E14. The install is a job in the sandbox now, so a pass that runs
    out of patience hands the slot back instead of holding the lock --
    but it must not run the leg either. A model that wakes into a tree
    whose `pip install` is half done runs its tests against missing
    imports and concludes the dependencies are broken, which is the
    failure env_setup runs before the first leg to prevent."""
    log = []
    _wire(monkeypatch, log, healthy=True,
          setup={"execution_id": 192, "action": "setup_running",
                 "detail": "still installing as hjob-1"})

    got = _run(advance.advance_one(_Conn(log), _Driver(log), _row(),
                                   None, None, None))

    assert [a["action"] for a in got] == ["setup_running"]
    assert "leg" not in log, f"the leg ran during the install: {log}"
    assert log[-1] == "unlock", "the lock was held while the job ran"


def test_the_holder_can_still_destroy_a_sandbox_that_is_really_dead(
        monkeypatch):
    """The recovery path is not what was wrong and must survive the fix. A
    worker that HOLDS the lock and finds a dead bridge still destroys the
    pod so dispatch rebuilds it — the claim, and the tree, survive."""
    log = []
    _wire(monkeypatch, log, healthy=False)
    conn, driver = _Conn(log), _Driver(log)

    got = _run(advance.advance_one(conn, driver, _row(), None, None, None))

    assert driver.destroyed == ["pod-192"]
    assert [a["action"] for a in got] == ["unreachable"]
    assert any("container_id = NULL" in sql for sql in conn.executed)
    assert log[-1] == "unlock"


def test_a_held_lock_is_released_when_the_leg_raises(monkeypatch):
    """An advisory lock dies with its connection, but the connection is
    pooled and goes straight back to another leg. A failure path that
    skipped the release would wedge the execution for the pool's life."""
    log = []
    _wire(monkeypatch, log, healthy=True, leg=RuntimeError("provider blew up"),
          setup={"execution_id": 192, "action": "setup"})

    got = _run(advance.advance_one(_Conn(log), _Driver(log), _row(),
                                   None, None, None))

    assert log[-1] == "unlock", f"the lock was not released: {log}"
    assert [a["action"] for a in got] == ["setup", "error"], (
        "the setup that did happen was thrown away with the failure")
    assert "provider blew up" in got[-1]["detail"]


def test_a_held_lock_is_released_when_the_probe_raises(monkeypatch):
    """Same rule, earlier failure: the reachability check is inside the
    lock now, so it is inside what has to be unwound."""
    log = []
    _wire(monkeypatch, log, healthy=True)

    async def _boom(conn, driver, row):
        log.append("probe")
        raise OSError("the driver went away")

    monkeypatch.setattr(advance, "ensure_reachable", _boom)
    got = _run(advance.advance_one(_Conn(log), _Driver(log), _row(),
                                   None, None, None))

    assert log == ["lock", "probe", "unlock"], log
    assert got[-1]["action"] == "error"


# ── C3 · the slot rotates instead of being re-won by the lowest id ───────

class _Runnable:
    """RUNNING rows plus what the journal says about each one's activity."""

    def __init__(self, ids):
        self.last = {i: None for i in ids}

    async def fetch(self, sql, *args):
        return [{"id": i, "kind": "story", "domain": "code", "generation": 0,
                 "container_id": f"c{i}", "terminal_command": None,
                 "task": "", "workspace_source": None,
                 "workspace_path": "/w", KEY: self.last[i]}
                for i in sorted(self.last)]

    def journalled(self, execution_id, when):
        self.last[execution_id] = when


def test_one_slot_over_three_passes_advances_all_three_stories():
    """THE TEST THAT WOULD HAVE CAUGHT THE STARVATION. Under `ORDER BY
    e.id` this returns [7, 7, 7] and stories 55 and 101 never run."""
    db = _Runnable([101, 7, 55])
    advanced = []
    for pass_no in range(4):
        winner = _run(worker_loop.claimable(db))[0]["id"]   # one slot
        advanced.append(winner)
        db.journalled(winner, NOW + dt.timedelta(minutes=pass_no))

    assert sorted(advanced[:3]) == [7, 55, 101], (
        f"the slot did not rotate: {advanced}")
    assert advanced[3] == advanced[0], (
        "the fourth pass did not come back to the least recently advanced")


def test_a_run_that_has_never_advanced_is_offered_the_slot_first():
    """A freshly dispatched execution has an empty journal. Treating "no
    activity" as "infinitely recent" would starve exactly the runs that
    have never run at all."""
    rows = [{"id": 1, KEY: NOW}, {"id": 900, KEY: None}]
    ranked = fairness.least_recently_advanced(rows)
    assert [r["id"] for r in ranked] == [900, 1]


def test_equally_idle_runs_are_ordered_the_same_way_by_every_replica():
    """Determinism is not decoration: two replicas ordering one row set
    differently would hand the same slot to different executions and undo
    the rotation. `admit.py` makes the same argument about its FIFO."""
    rows = [{"id": 9, KEY: NOW}, {"id": 2, KEY: NOW}, {"id": 5, KEY: NOW}]
    ids = [r["id"] for r in fairness.least_recently_advanced(rows)]
    assert ids == [2, 5, 9]
    assert [r["id"] for r in
            fairness.least_recently_advanced(list(reversed(rows)))] == ids


def test_the_scheduling_key_does_not_travel_with_the_row():
    """The row goes on to the executor, the sandbox spec and the
    executor's context, all of which treat it as a bag of plain values. A
    timestamp only the scheduler cares about has no business going there."""
    rows = _run(worker_loop.claimable(_Runnable([3])))
    assert KEY not in rows[0], f"{KEY} leaked onto the executor's row"


# ── C4 · admission is bounded per pass ───────────────────────────────────

class _Queue:
    """A PENDING queue that ignores LIMIT, so the ceiling is what is tested.

    Deliberate: if this honoured `LIMIT $2` the test would prove the query
    was spelled a certain way. The bound that has to hold is on what gets
    ADMITTED, and it has to hold even when the select returns more.
    """

    def __init__(self, pending):
        self.pending, self.admitted = list(pending), []

    async def fetch(self, sql, *args):
        # `story_id` because admission resolves WHO owns the run from
        # it; a row without one is attributed to the unknown bucket.
        return [{"id": i, "kind": "story", "domain": "code", "goal_id": "g",
                 "story_id": f"s{i}"}
                for i in self.pending]

    async def fetchval(self, sql, *args):
        if "count(*) FROM admission_slots" in sql:
            # The CROSS-GOAL ceiling, which is a count rather than a
            # second slot claim -- one slot per execution is a unique
            # index, so claiming two broke admission entirely. Nothing is
            # held in this test; the batch bound is what it measures.
            return 0
        return 4242                           # a free slot, every time

    async def execute(self, sql, *args):
        if "ADMITTED" in sql:
            self.admitted.append(args[0])
            self.pending.remove(args[0])


@pytest.fixture()
def batch(monkeypatch):
    """Set AGENTOS_ADMIT_BATCH for one test; monkeypatch puts it back."""
    def _set(value):
        monkeypatch.delenv("AGENTOS_ADMIT_BATCH", raising=False)
        if value is not None:
            monkeypatch.setenv("AGENTOS_ADMIT_BATCH", value)
    _set(None)
    return _set


@pytest.fixture()
def providers_up(monkeypatch):
    async def _none(conn):
        return []
    monkeypatch.setattr(admit_mod, "open_models", _none)


def test_one_pass_admits_a_batch_and_the_rest_stay_pending(providers_up,
                                                          batch):
    """~40 admitted at once became ~40 pods, ~36 idle, each holding a PVC.
    What is left over is not refused — admission is level-triggered, so the
    bound is a rate and the next tick takes the next batch, still FIFO."""
    batch("5")
    queue = _Queue(range(1, 13))
    assert _run(admit_mod.admit_pending(queue, "g")) == [1, 2, 3, 4, 5]
    assert len(queue.pending) == 7, "the overflow was dropped, not deferred"
    assert _run(admit_mod.admit_pending(queue, "g")) == [6, 7, 8, 9, 10]
    assert queue.pending == [11, 12]


def test_the_batch_is_configuration_with_a_default_somebody_chose(batch):
    """The house rule: no default hidden inside a two-argument
    `environ.get`, and nonsense falls back rather than restoring the
    unbounded behaviour this exists to remove."""
    assert admit_batch() == DEFAULT_ADMIT_BATCH == 24
    batch("3")
    assert admit_batch() == 3
    batch("99999")
    assert admit_batch() == MAX_ADMIT_BATCH
    for nonsense in ("banana", "0", "-7", "   "):
        batch(nonsense)
        assert admit_batch() == DEFAULT_ADMIT_BATCH


def test_admission_still_refuses_while_a_model_is_down(monkeypatch, batch):
    """Fb4b, unregressed. A slot claimed during an outage is a story that
    dispatches, stalls on ProviderUnavailable and holds the slot for the
    length of the downtime — worse than waiting."""
    async def _down(conn):
        return [{"model": "sonnet", "kind": "overloaded", "age_s": 12.0}]

    monkeypatch.setattr(admit_mod, "open_models", _down)
    batch("5")
    queue = _Queue(range(1, 13))
    assert _run(admit_mod.admit_pending(queue, "g")) == []
    assert queue.admitted == [] and len(queue.pending) == 12
