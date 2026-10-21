"""Blocking work must not share one pool nobody sized.

R1. There was no `set_default_executor` anywhere, so every
`asyncio.to_thread` and `run_in_executor(None, ...)` shared a pool sized
`min(32, os.cpu_count() + 4)`. `os.cpu_count()` reports the NODE, not the
container's 1-CPU cgroup, so the number was 12 on an 8-vCPU node and 6 on
a 2-vCPU one -- whatever machine the pod landed on.

THE MEASURED FAILURE. A tool call holds its thread for the whole call
(615s for env_setup, 135s for a model `bash`) and there could be ten
legs deep in one at once. `dispatch_pass` then submits ~50
Kubernetes calls at once. They queue in the executor's unbounded
`SimpleQueue`; `k8s_call` deliberately has no `wait_for`; `dispatch_pass`
is awaited inline in `worker_tick`; the pass never returns; `alive.touch`
never fires; the liveness probe SIGKILLs the pod -- discarding in-flight
legs, which the peer replica re-claims and repeats.

Moving the verifier's tar and git work off the event loop on the same day
added load to this same pool, which is why R1 is the other half of that
change rather than a separate improvement.

TWO POOLS AND NOT ONE BIGGER ONE, because a bigger shared pool leaves the
coupling intact: a slow `bash` would still compete with dispatch, and the
worst case would merely need more legs to reproduce.

AND D5 CUT THE LAST COUPLING IN THE OTHER DIRECTION. `tool_threads()`
used to be `MAX_LEG_CONCURRENCY + 6`, so another module's concurrency
knob silently resized this pool -- in the verifier too, which imports
`executors` and runs no legs at all. E14 removed the reason: every call
that could be long is a job now, `limits.bridge_timeout` bounds the rest
from the caller's side, and the pool is a flat 16. These tests therefore
assert the BOUND (no bridge call may outlast what the pool was sized
for) rather than an arithmetic between two constants.
"""
from __future__ import annotations

import ast
import pathlib

from kernel import executors


def test_the_tool_pool_does_not_derive_from_a_leg_ceiling():
    """D5. A pool sized from `kernel.worker.concurrency` is a pool that a
    soak setting can resize without anybody deciding to.

    OVER THE PARSED TREE, not the text. The file still SAYS
    `MAX_LEG_CONCURRENCY` in prose, deliberately -- a retired coupling
    that leaves no trace of why it went is re-introduced by the next
    person who needs a number. What must be gone is the import.
    """
    tree = ast.parse(
        pathlib.Path("kernel/executors.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.startswith("kernel.worker") for m in imported), imported


def test_the_tool_pool_outnumbers_the_calls_that_can_be_outstanding():
    """Sixteen threads against a fan-out whose default is four legs, each
    holding at most one bridge call, plus the `bridge_health` probes a
    dispatch fires at pods that are not answering yet."""
    from kernel.worker.concurrency import DEFAULT_LEG_CONCURRENCY

    assert executors.tool_threads() > DEFAULT_LEG_CONCURRENCY


def test_no_bridge_call_can_outlast_what_the_pool_was_sized_for():
    """THE PRECONDITION D5 INHERITED (plan 12.7). A flat pool is only
    defensible while every bridge call is short: the moment one can block
    for minutes again, sixteen threads is a wedge and not a size.
    `job_poll` is 10s, anything without a timeout is 30s, and the model's
    own foreground `bash` is the longest hold that exists."""
    from kernel.bridge import limits

    longest = limits.BASH_MAX_S + limits.BRIDGE_MARGIN_S
    assert longest == 135
    assert limits.POLL_MAX_S <= 10
    assert limits.BRIDGE_DEFAULT_S <= 30
    assert limits.bridge_timeout(limits.JOB_START,
                                 {"budget_s": limits.JOB_MAX_S}) <= 30, (
        "a job START must never carry the job's budget -- that would put "
        "a two-hour hold back in the pool D5 just stopped sizing for it")


def test_infra_absorbs_a_full_dispatch_burst():
    """`dispatch_pass` runs up to 24 dispatches, each at least two
    Kubernetes calls."""
    assert executors.infra_threads() >= 48


def test_the_pools_are_separate_objects():
    """The whole point: a ten-minute sandbox must not be able to delay a
    single Kubernetes call."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.new_event_loop()
    try:
        executors.install(loop)
        default = loop._default_executor
        assert isinstance(default, ThreadPoolExecutor)
        assert executors.tools() is not default
    finally:
        executors.shutdown()
        loop.close()


def test_the_size_is_overridable_and_says_its_default(monkeypatch):
    """`kernel/` bans `environ.get(x, default)`; the explicit form has to
    behave, including when somebody types a word."""
    monkeypatch.setenv("AGENTOS_INFRA_THREADS", "7")
    assert executors.infra_threads() == 7
    monkeypatch.setenv("AGENTOS_INFRA_THREADS", "not-a-number")
    assert executors.infra_threads() == 64
    monkeypatch.setenv("AGENTOS_INFRA_THREADS", "0")
    assert executors.infra_threads() == 1


def test_shutdown_is_idempotent():
    executors.tools()
    executors.shutdown()
    executors.shutdown()


# -- the wiring, which is the part that actually matters ----------------

KERNEL = pathlib.Path(executors.__file__).parent


def _calls(path: pathlib.Path, name: str) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == name]


def test_the_bridge_never_uses_the_default_executor():
    """`run_in_executor(None, ...)` is the bug. Both call sites in the
    bridge must name the tool pool."""
    offenders = []
    for call in _calls(KERNEL / "bridge" / "client.py", "run_in_executor"):
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and first.value is None:
            offenders.append(call.lineno)
    assert offenders == [], (
        f"run_in_executor(None, ...) at lines {offenders} -- tool calls "
        f"would share the pool with dispatch again")


def test_the_service_installs_the_pools_before_it_runs():
    src = (KERNEL / "worker" / "service.py").read_text(encoding="utf-8")
    assert "executors.install(" in src
    # Before the database wait, because everything after it may await.
    assert src.index("executors.install(") < src.index("wait_for_pool(dsn)")
    assert "executors.shutdown()" in src
