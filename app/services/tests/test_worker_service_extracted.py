"""The execution plane is its own process, and the console is a view.

WHY THIS MATTERS ENOUGH TO PIN. `_worker_forever` used to start as a
side effect of `kernel/console/server.py:serve()` binding port 8770, so:
the only way to run a story was to start a debug HTTP server; two
conductors were impossible, because a loop that starts on a port bind
cannot be replicated; and deleting an unauthenticated surface would have
deleted the execution plane with it.

These tests fail if any of that comes back.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import pathlib

import pytest

from kernel.worker import service


def _src(rel: str) -> str:
    root = pathlib.Path(__file__).resolve().parents[3]
    return io.open(root / rel, encoding="utf-8").read()


def test_the_console_does_not_start_the_worker():
    """AST, not text. The docstring in `serve()` explains why the loop
    left, and naming it there must not fail the test that keeps it out --
    a guard that fires on the comment describing it is a guard people
    delete."""
    import ast

    tree = ast.parse(_src("kernel/console/server.py"))
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_worker_forever" not in defined, (
        "the worker loop is back in the console -- starting the execution "
        "plane by binding a debug port is what this move undid")
    assert "worker_pass" not in defined, (
        "worker_pass is back in the console; it belongs to the worker")

    # The console still serves. It just does not sweep.
    assert "serve" in defined


def test_there_is_one_construction_site_for_an_executor():
    """There were three, and they had already drifted.

    Two lived as closures in the console and one in the worker. The
    console's START path built its envelope with NO `identity`, so a run
    driven from there committed without attribution and nothing said so
    -- the same shape as the two worker loops that preceded this module,
    one level down.

    NOTE, deliberately not asserted away: the console still drives a run
    when a person clicks start (`_drive`, two of them). That is a real
    second execution path and it is Phase 4's problem, not this one --
    but it now goes through the SAME builder, so it can no longer drift.
    """
    import ast

    console = _src("kernel/console/server.py")
    tree = ast.parse(console)
    inner = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "make_executor" in inner, "the console no longer builds executors?"

    calls = {ast.unparse(n.func) for n in ast.walk(tree)
             if isinstance(n, ast.Call)}
    assert "build_executor" in calls, (
        "the console builds an executor inline again -- three copies is "
        "what this collapsed")
    assert "BridgeToolExecutor" not in calls, (
        "the console constructs a BridgeToolExecutor directly again")

    shared = _src("kernel/worker/executor_pass.py")
    assert "def build_executor(" in shared
    assert shared.count("BridgeToolExecutor(") == 1, (
        "more than one place constructs the executor")


def test_the_service_is_startable_by_name():
    """`python -m kernel.worker.service` must be the documented way in."""
    root = pathlib.Path(__file__).resolve().parents[3]
    assert (root / "kernel" / "worker" / "service.py").is_file()
    assert hasattr(service, "main") and inspect.iscoroutinefunction(service.main)
    src = _src("kernel/worker/service.py")
    assert '__name__ == "__main__"' in src, "no module entrypoint"


def test_something_actually_starts_it():
    """A service nobody launches is the defect this replaced, not a fix."""
    sh = _src("infra/e0/worker.sh")
    assert "python3 -m kernel.worker.service" in sh
    ps1 = _src("local-development/scripts/run/kernel-stack.ps1")
    assert 'Invoke-Bash "worker.sh" "restart"' in ps1, (
        "the local launcher starts the console but not the worker, which is "
        "a stack that accepts stories and never runs them")
    assert 'Invoke-Bash "worker.sh" "stop"' in ps1, (
        "the stop path leaves the worker running")


def test_it_installs_a_sigterm_handler():
    """On Spot, 15 seconds of grace is worth nothing without one."""
    src = _src("kernel/worker/service.py")
    assert "SIGTERM" in src
    assert "add_signal_handler" in src


@pytest.mark.parametrize("interval", [0.01])
def test_run_forever_stops_when_asked(interval, monkeypatch):
    """The old loop was `while True` and could only be killed."""
    passes = []

    async def fake_pass(pool, driver, adapter, supervisor=None,
                        adjudicator=None):
        # `adjudicator` arrived when adjudication moved off the serial
        # tick. Keeping the fake's signature in step with the real one is
        # the point of this test failing when it drifts.
        passes.append(1)
        return []

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Conn()

    monkeypatch.setitem(
        __import__("sys").modules, "kernel.worker.executor_pass",
        type("m", (), {"worker_pass": fake_pass}))
    # A FAKE DRIVER, NOT A BARE OBJECT. run_forever preflights its driver
    # before entering the loop, and `object()` has no preflight — so this
    # test failed on a change that was correct. The fix is not to make
    # the call defensive with getattr: a guard that skips itself for
    # anything not implementing it is a guard that does not run.
    #
    # It borrows `Driver.preflight` rather than defining its own no-op,
    # so if that contract changes the double follows instead of drifting
    # into agreeing by coincidence.
    from kernel.driver import Driver

    fake_driver = type("FakeDriver", (), {"preflight": Driver.preflight})()
    monkeypatch.setattr(
        "kernel.drivers.registry.make", lambda *a, **k: fake_driver)
    monkeypatch.setattr(
        "kernel.harness.models.get_adapter", lambda *a, **k: object())
    monkeypatch.setattr(
        "kernel.credentials.sweep.sweep",
        lambda *a, **k: asyncio.sleep(0))

    async def drive():
        stopping = asyncio.Event()
        task = asyncio.create_task(
            service.run_forever(_Pool(), interval=interval, stopping=stopping))
        await asyncio.sleep(0.08)
        stopping.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(drive())
    assert passes, "the loop never ran a pass"


def test_the_pool_is_configured_in_one_place():
    """Two copies of the pool settings is two things to forget to change
    -- and one of them is a security boundary (SET ROLE agentos_worker)."""
    bridge = _src("kernel/loopbridge.py")
    # `wait_for_pool` and the bounded close left `service.py` with the
    # rest of the pool lifetime (D2a), so the claim is about
    # `service_pool.py` now -- and it has to be about EVERY service
    # module, because a second `create_pool` anywhere in this process
    # is a second copy of the role gate (SET ROLE agentos_worker) and
    # the search_path.
    assert "async def make_pool" in bridge
    assert "make_pool" in _src("kernel/worker/service_pool.py")
    for rel in ("kernel/worker/service.py",
                "kernel/worker/service_pool.py",
                "kernel/worker/service_tick.py",
                "kernel/worker/service_wake.py",
                "kernel/worker/service_drain.py"):
        assert "create_pool" not in _src(rel), (
            f"{rel} builds its own pool; the role gate and search_path "
            "now exist in two places")
