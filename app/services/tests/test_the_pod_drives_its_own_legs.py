"""D2's assembly: the leg loop, in the pod.

D2 built every part and started none of them. This is the caller, and
its correctness is almost entirely in when it declines to act -- a loop
that runs a leg it should not have is two writers on one story, and a
loop that stops when it should have waited is a story that never
finishes with nobody saying why.

IT DELIBERATELY REUSES `advance_one`. That function already does one leg
under one lock -- reachability, setup, leg -- and was factored out of
`worker_tick` precisely so two paths could not drift. The worker's own
history is the argument: `worker_pass` was a second `worker_tick` that
drifted until three features were unreachable from the console.
"""
from __future__ import annotations

import asyncio

import pytest

from kernel.worker import pod_loop


class _Conn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, sql, *args):
        return self._row


class _Pool:
    def __init__(self, row):
        self._conn = _Conn(row)

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


# `executor_factory(conn)` rather than a prebuilt maker: `build_executor`
# is synchronous and takes the connection it will use, and the loop
# acquires a fresh one per turn. A maker built once would close over a
# connection already returned to the pool.
def _factory(seen=None):
    def make(conn):
        if seen is not None:
            seen.append(conn)
        return lambda profile, row, plan_approved=None: None
    return make


def _advance(calls, actions=None):
    async def fake(conn, driver, row, run_leg_fn, make_executor,
                   adapter, **kw):
        calls.append(row["id"])
        return actions if actions is not None else [{"action": "leg"}]
    return fake


def _once(row, monkeypatch, calls, actions=None):
    import kernel.worker.advance as advance
    monkeypatch.setattr(advance, "advance_one", _advance(calls, actions))
    return asyncio.run(pod_loop.once(_Pool(row), None, 7, None,
                                     _factory(), None))


# ── when it runs a leg ─────────────────────────────────────────────────

def test_a_running_execution_gets_its_leg(monkeypatch):
    calls = []
    actions, keep_going = _once({"id": 7, "state": "RUNNING"},
                                monkeypatch, calls)

    assert calls == [7]
    assert keep_going and actions


# ── when it must NOT ───────────────────────────────────────────────────

@pytest.mark.parametrize("state", pod_loop.TERMINAL)
def test_a_terminal_execution_ends_the_loop(state, monkeypatch):
    """The pod EXITS rather than idling. It is this execution's pod; a
    worker still looping on a finished row holds its admission slot and
    its workspace claim for as long as the pod lives."""
    calls = []
    actions, keep_going = _once({"id": 7, "state": state}, monkeypatch, calls)

    assert calls == [], f"it ran a leg on a {state} execution"
    assert keep_going is False


@pytest.mark.parametrize("state", ["VERIFYING", "PARKED", "DISPATCHING",
                                   "ADMITTED", "PENDING"])
def test_a_state_somebody_else_owns_is_waited_out(state, monkeypatch):
    """Waiting is not idleness. VERIFYING belongs to the verifier,
    PARKED to a human, DISPATCHING to the conductor -- racing any of
    them for the row is how two writers disagree about one story."""
    calls = []
    actions, keep_going = _once({"id": 7, "state": state}, monkeypatch, calls)

    assert calls == []
    assert keep_going is True, "it stopped instead of waiting"


def test_a_missing_row_ends_the_loop(monkeypatch):
    """The execution was deleted or never existed. Nothing to drive, and
    retrying forever would be a pod burning ticks on a row that is not
    coming back."""
    calls = []
    actions, keep_going = _once(None, monkeypatch, calls)

    assert calls == [] and keep_going is False


# ── the lifetime ───────────────────────────────────────────────────────

def test_one_leg_that_raises_does_not_kill_the_worker(monkeypatch):
    """A transient error is one leg. A worker that exited on the first
    would turn a flaky model call into a destroyed pod."""
    import kernel.worker.advance as advance

    seen = []

    async def boom(conn, driver, row, *a, **kw):
        seen.append(row["id"])
        if len(seen) <= 2:
            raise RuntimeError("transient")
        return [{"action": "leg"}]

    monkeypatch.setattr(advance, "advance_one", boom)
    stopping = asyncio.Event()

    async def drive():
        task = asyncio.create_task(pod_loop.run_forever(
            _Pool({"id": 7, "state": "RUNNING"}), None, None,
            _factory(), None, eid=7, stopping=stopping, idle=0.01))
        await asyncio.sleep(0.08)
        stopping.set()
        await task

    asyncio.run(drive())
    assert len(seen) > 2, "the loop died on a transient failure"


def test_a_loop_that_fails_every_turn_GIVES_UP(monkeypatch):
    """THE GAP THIS CLOSES, and it was mine.

    The first draft never raised out of the loop. But a pod whose podfs
    answers is a pod the conductor PROMOTES -- readiness passes, the
    execution goes RUNNING, and `report_stuck` only looks at PENDING and
    ADMITTED. So a permanently broken leg loop held an admission slot on
    a story NOTHING WOULD EVER REAP, looking healthy throughout.

    Exiting turns it into a CrashLoopBackOff, which the
    never-became-Ready path already handles: promotion gives up at
    STARTUP_BUDGET_S, destroys the pod and releases the execution with a
    reason. A loud failure the system can clean up beats a quiet one it
    cannot.
    """
    import kernel.worker.advance as advance

    tries = []

    async def always_boom(conn, driver, row, *a, **kw):
        tries.append(row["id"])
        raise RuntimeError("the deployment is broken")

    monkeypatch.setattr(advance, "advance_one", always_boom)

    async def drive():
        await pod_loop.run_forever(
            _Pool({"id": 7, "state": "RUNNING"}), None, None,
            _factory(), None, eid=7, stopping=asyncio.Event(), idle=0.001)

    with pytest.raises(RuntimeError):
        asyncio.run(drive())

    assert len(tries) == pod_loop.MAX_CONSECUTIVE_FAILURES, (
        f"gave up after {len(tries)}, expected "
        f"{pod_loop.MAX_CONSECUTIVE_FAILURES}")


def test_the_failure_count_is_CONSECUTIVE_and_not_cumulative(monkeypatch):
    """A story that hits one bad leg an hour is working. Counting
    cumulatively would kill a long, healthy run."""
    import kernel.worker.advance as advance

    calls = []
    stopping = asyncio.Event()

    # STOPPED BY THE CONDITION, NOT BY THE CLOCK. This ran the loop for
    # a flat 0.1 seconds and asserted a minimum number of turns, so on a
    # loaded machine -- a full suite run, where it failed twice in a row
    # while passing alone -- it simply did not get enough turns, and the
    # failure said "the counter is cumulative" about a counter that was
    # fine. A test whose verdict depends on how busy the host is cannot
    # tell you anything about the code.
    target = pod_loop.MAX_CONSECUTIVE_FAILURES * 2 + 2

    async def every_other(conn, driver, row, *a, **kw):
        calls.append(1)
        if len(calls) >= target:
            stopping.set()
        if len(calls) % 2:
            raise RuntimeError("alternating")
        return [{"action": "leg"}]

    monkeypatch.setattr(advance, "advance_one", every_other)

    async def drive():
        task = asyncio.create_task(pod_loop.run_forever(
            _Pool({"id": 7, "state": "RUNNING"}), None, None,
            _factory(), None, eid=7, stopping=stopping, idle=0.001))
        # The loop either reaches `target` and stops itself, or it gives
        # up early -- the bug -- and `run_forever` returns on its own.
        # The timeout is a backstop against a hang, not a pacing device,
        # so it is far longer than the work needs.
        await asyncio.wait_for(task, timeout=30)

    asyncio.run(drive())
    assert len(calls) >= target, (
        f"a run alternating success and failure gave up after "
        f"{len(calls)} turns; the counter is cumulative rather than "
        f"consecutive")


def test_it_refuses_to_guess_which_execution_it_is(monkeypatch):
    """A worker that scanned for work would be a second scheduler racing
    admission. `kernel/` bans `environ.get(x, default)` and the reason
    lands exactly here: a default would be an execution id nobody chose,
    and this process would drive somebody else's story."""
    monkeypatch.delenv(pod_loop.EXECUTION_ENV, raising=False)

    with pytest.raises(pod_loop.Unnamed):
        pod_loop.execution_id()


def test_it_reads_the_execution_from_the_manifest_variable(monkeypatch):
    monkeypatch.setenv(pod_loop.EXECUTION_ENV, "42")
    assert pod_loop.execution_id() == 42


def test_the_executor_is_built_against_the_turns_own_connection(monkeypatch):
    """`build_executor` is synchronous and takes the connection it will
    use. This loop acquires a fresh one per turn, so a maker built once
    at startup would close over a connection long since returned to the
    pool -- and the pod has TWO, not sixteen, so reuse is not a
    theoretical concern."""
    import kernel.worker.advance as advance

    seen = []
    monkeypatch.setattr(advance, "advance_one", _advance([]))
    pool = _Pool({"id": 7, "state": "RUNNING"})

    asyncio.run(pod_loop.once(pool, None, 7, None, _factory(seen), None))

    assert len(seen) == 1
    assert seen[0] is pool._conn, "the executor was built against some "        "other connection than the one the leg will use"


# ── the container must go down with the leg loop ───────────────────────

def test_pod_main_exits_the_process_when_the_leg_loop_dies():
    """MEASURED ON EXECUTION 212, and the module docstring claimed the
    opposite was already true.

    `_drive` builds the pool BEFORE `run_forever`, so an
    InvalidPasswordError from `make_pool` raised out of it rather than
    into the loop's retry. podfs runs in a NON-DAEMON thread, so the
    interpreter waited on a `serve_forever` that never returns: the
    process hung instead of exiting, worker `ready=true`, `restarts=0`,
    and no leg loop at all. A pod Ready with a dead leg holds its
    admission slot and is reaped by nothing.

    EVERY NAME IT USES IS RESOLVED, not just present as text. The first
    version of this test asserted `"os._exit" in src` -- and PASSED on
    execution 213, where the exit path raised `NameError: name 'sys' is
    not defined` because the import edit had silently not matched. A
    string-presence check on code that cannot run is the same vacuous
    pass 11.5f describes, written by me while fixing it.
    """
    import ast
    import inspect

    from kernel.worker import pod_main

    src = inspect.getsource(pod_main._main)
    assert "os._exit" in src, (
        "pod_main no longer forces the process down when the leg loop "
        "dies; the non-daemon podfs thread will hold it up")
    assert "except BaseException" in src, (
        "the exit path misses the failure that was actually measured -- "
        "make_pool raises before run_forever's retry can see it")

    # THE PART THAT WOULD HAVE CAUGHT IT: every module-level name the
    # exit path touches must actually be imported.
    tree = ast.parse(inspect.getsource(pod_main))
    imported = {a.asname or a.name.split(".")[0]
                for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
    imported |= {a.asname or a.name
                 for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 for a in n.names}
    for name in ("os", "sys"):
        assert name in imported, (
            f"the exit path calls {name}.* and {name} is not imported at "
            f"module level -- it will raise NameError at the moment it "
            f"is needed, which is the moment nothing else is watching")


# ── reachability: the pod must not ask the cluster about itself ────────

def test_the_pod_resolves_its_bridge_without_the_cluster_api():
    """MEASURED ON EXECUTION 214.

    `ensure_reachable` reads the pod object from the KUBERNETES API and
    may DESTROY it. From inside the pod that call is routed through the
    egress proxy, which refuses it (`Tunnel connection failed: 403
    Forbidden`) -- and it would fail unproxied too, because
    `agentos-execution` is bound to NO ROLE AT ALL. That is deliberate:
    it is the identity a model's `bash` shares a network namespace with.

    `local_bridge` must therefore touch neither the driver nor the
    connection. Passing None for both is the assertion: anything that
    reached for either would raise.
    """
    import asyncio

    got = asyncio.run(pod_loop.local_bridge(None, None, {}))

    from kernel.bridge.client import BRIDGE_PORT
    assert got == BRIDGE_PORT


def test_it_never_reports_the_pod_unreachable_to_itself():
    """The probe it replaces is destructive. A pod that concludes its own
    sandbox is dead and deletes itself has nothing left to report why --
    deciding a pod is unreachable is the conductor's job, from outside,
    where there is still a row to write the reason on."""
    import asyncio

    for row in ({}, {"bridge_port": None}, {"bridge_port": 0}):
        assert asyncio.run(pod_loop.local_bridge(None, None, row)) is not None


def test_the_loop_passes_its_own_step_and_not_the_conductors(monkeypatch):
    """The whole fix. `advance_one` is still ONE function -- the C5
    funnel -- and the caller supplies the policy."""
    import asyncio

    import kernel.worker.advance as advance

    seen = {}

    async def fake(conn, driver, row, run_leg_fn, make_executor, adapter,
                   *, reachable=None):
        seen["reachable"] = reachable
        return []

    monkeypatch.setattr(advance, "advance_one", fake)
    asyncio.run(pod_loop.once(_Pool({"id": 7, "state": "RUNNING"}), None, 7,
                              None, _factory(), None))

    assert seen["reachable"] is pod_loop.local_bridge, (
        "the pod loop uses the conductor's reachability probe, which "
        "asks the cluster API from inside the pod")


def test_the_conductor_keeps_the_probe_it_had():
    """INJECTED, NOT BRANCHED. `advance_one` defaults to
    `ensure_reachable`, so the conductor's behaviour is unchanged and
    there is no `if is_pod(driver)` for D3 to delete later."""
    import inspect

    from kernel.worker.advance import advance_one

    assert inspect.signature(advance_one).parameters["reachable"].default \
        is None
    src = inspect.getsource(inspect.getmodule(advance_one))
    assert "reachable or ensure_reachable" in src
    # COMMENT LINES STRIPPED, for the third time today. The note above
    # the fix says "rather than `if is_pod(driver)`", and an absence
    # assertion fires happily on the sentence explaining the absence.
    code = chr(10).join(line for line in src.splitlines()
                        if not line.lstrip().startswith("#"))
    assert "is_pod(" not in code, (
        "a substrate branch was added where a parameter was enough")


def test_the_verifier_is_untouched_by_any_of_this():
    """Asked for explicitly. The verifier judges claims; it never runs a
    leg, so it never reaches `advance_one` and cannot inherit this
    change. Asserted because 'it should not affect X' is the kind of
    claim that rots silently."""
    import ast
    import pathlib

    for rel in ("kernel/verifier/service.py", "kernel/verifier/adjudicator.py",
                "kernel/verifier/verify.py"):
        src = pathlib.Path(rel).read_text(encoding="utf-8")
        called = {n.func.attr if isinstance(n.func, ast.Attribute)
                  else getattr(n.func, "id", "")
                  for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)}
        for name in ("advance_one", "ensure_reachable", "local_bridge"):
            assert name not in called, f"{rel} reaches {name}"
