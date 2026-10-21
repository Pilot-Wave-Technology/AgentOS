"""Nothing may block the event loop on an unbounded Kubernetes call.

MEASURED. Both conductor pods wedged for thirty minutes: all three
loops frozen, `error_count = 0`, pods READY 2/2, nothing in the log.
`loop_heartbeats` had it exactly right -- `worker.pass` stale 0:29:55 at
tick 825 -- and nothing read it. Recovery was a person noticing.

Three independent faults had to line up, and all three are covered here:

  1. zero of thirteen API calls set `_request_timeout`, so the client
     waited for ever
  2. ten of thirteen ran ON the event loop, so one hung call froze every
     coroutine in the process rather than one loop
  3. nothing consumed `loop_heartbeats` and there was no liveness probe,
     so it stayed invisible until a human looked

The AST audit is the one that matters most. Fixing thirteen call sites
is worth nothing if the fourteenth is written the old way, and the old
way is the shorter thing to type.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import time

import pytest

from kernel.drivers import k8s_call
from kernel.obs import alive

DRIVERS = pathlib.Path(k8s_call.__file__).resolve().parent

# How the generated client names the calls that reach the API server.
_PREFIXES = ("create_namespaced", "read_namespaced", "delete_namespaced",
             "list_namespaced", "patch_namespaced", "replace_namespaced",
             "connect_get_namespaced", "connect_post_namespaced")


def _reaches_the_api(name: str) -> bool:
    return name.startswith(_PREFIXES)


def _direct_invocations(tree: ast.AST) -> list[str]:
    """API methods that are CALLED rather than handed to the wrapper.

    Going through `k8s_call.call` means passing the bound method as a
    value -- `call(core.list_namespaced_pod, ...)`. So any place the
    method is itself the callee of a Call node is a site that bypassed
    the wrapper, whatever else it does.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and _reaches_the_api(func.attr):
            out.append(f"{func.attr} at line {node.lineno}")
    return out


def test_no_kubernetes_call_bypasses_the_wrapper():
    """Site fourteen is the one this is for."""
    offenders = {}
    for path in sorted(DRIVERS.glob("*.py")):
        if path.name == "k8s_call.py":
            continue
        found = _direct_invocations(
            ast.parse(path.read_text(encoding="utf-8")))
        if found:
            offenders[path.name] = found
    assert offenders == {}, (
        "these call the Kubernetes API directly instead of through "
        "kernel.drivers.k8s_call.call/quiet, so they are unbounded and "
        f"block the event loop: {offenders}")


def test_at_least_the_known_call_sites_are_still_covered():
    """Guards the audit itself: a pattern that matches nothing passes."""
    seen = 0
    for path in sorted(DRIVERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and _reaches_the_api(node.attr)):
                seen += 1
    assert seen >= 13, f"the audit found only {seen} API references"


# -- fault 1: the call is bounded ---------------------------------------

@pytest.mark.asyncio
async def test_every_call_carries_a_connect_and_a_read_timeout():
    """A tuple, not a scalar. A scalar sets the read timeout alone and
    leaves a dropped SYN hanging exactly as before."""
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return "ok"

    assert await k8s_call.call(fake, namespace="n") == "ok"
    connect, read = seen["_request_timeout"]
    assert connect == k8s_call.CONNECT_S
    assert read == k8s_call.DEFAULT_S
    assert seen["namespace"] == "n"


@pytest.mark.asyncio
async def test_a_caller_may_ask_for_a_longer_read():
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)

    await k8s_call.call(fake, budget=k8s_call.LOGS_S)
    assert seen["_request_timeout"][1] == k8s_call.LOGS_S
    assert k8s_call.LOGS_S > k8s_call.DEFAULT_S


# -- fault 2: the loop keeps turning ------------------------------------

@pytest.mark.asyncio
async def test_a_slow_call_does_not_freeze_the_other_coroutines():
    """THE ACTUAL FAULT. One blocking call froze all three loops, which
    is why the symptom read as a dead process."""
    ticks = 0

    async def other_loop():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    def blocking(**kwargs):
        time.sleep(0.25)

    await asyncio.gather(k8s_call.call(blocking), other_loop())
    assert ticks == 10, "the event loop was blocked by the API call"


@pytest.mark.asyncio
async def test_a_failure_that_is_not_fatal_returns_none_and_says_so(caplog):
    def boom(**kwargs):
        raise RuntimeError("the control plane said no")

    got = await k8s_call.quiet(boom, what="reading logs")
    assert got is None
    assert "reading logs failed" in caplog.text


@pytest.mark.asyncio
async def test_a_failure_that_is_fatal_still_raises():
    def boom(**kwargs):
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        await k8s_call.call(boom)


# -- fault 3: a wedge becomes visible -----------------------------------

@pytest.fixture
def beats(tmp_path, monkeypatch):
    monkeypatch.setattr(alive, "BEAT_DIR", str(tmp_path))
    return tmp_path


def test_a_process_that_has_ticked_recently_is_healthy(beats):
    alive.touch("worker.pass")
    healthy, why = alive.verdict(120.0)
    assert healthy, why


def test_a_process_whose_loops_all_stopped_is_not(beats):
    alive.touch("worker.pass")
    healthy, why = alive.verdict(120.0, now=time.time() + 1800)
    assert not healthy
    assert "wedged" in why


def test_one_stalled_loop_is_no_longer_tolerated(beats):
    """THIS TEST USED TO ASSERT THE OPPOSITE, on reasoning that turned
    out to be wrong.

    It said: "restarting the pod over a SINGLE stalled loop would take
    out the healthy ones too. That case is an alert, not a restart."
    That assumed a restart is destructive. It is destructive only when
    the EVENT LOOP is wedged -- then the drain cannot run and in-flight
    legs are SIGKILLed. A single stalled pass is the other case: the loop
    is still turning, so SIGTERM drains the legs normally and the pod
    comes back with its reconcile working.

    And the cost of tolerating it is higher than it looked: a hung
    `reconcile_all` HOLDS THE ADVISORY LOCK, so the peer replica stops
    reconciling too. Nothing reclaims a PVC or an orphaned pod anywhere,
    until the namespace quota fills and no story can run at all.

    An unlabelled file has no declared interval and falls back to the
    floor, which is the mid-rollout case.
    """
    old = beats / "worker.reconcile"
    old.write_text("x")
    stale_at = time.time() - 3600
    os.utime(old, (stale_at, stale_at))
    alive.touch("worker.pass", every=4.0)
    healthy, why = alive.verdict(120.0)
    assert not healthy
    assert "worker.reconcile" in why


def test_a_process_that_has_not_started_yet_is_not_killed(beats):
    """initialDelaySeconds covers this window; failing here would
    restart every pod slow to reach its database."""
    healthy, why = alive.verdict(120.0)
    assert healthy and "starting" in why


def test_a_draining_process_reports_healthy(beats):
    """Otherwise the probe SIGKILLs every rollout part-way through its
    180s drain -- discarding the in-flight legs the drain exists to
    save."""
    alive.touch("worker.pass")
    alive.draining(True)
    healthy, why = alive.verdict(120.0, now=time.time() + 1800)
    assert healthy and "draining" in why


def test_the_drain_marker_is_not_mistaken_for_a_loop(beats):
    alive.draining(True)
    assert alive.newest_age_s() is None


def test_the_probe_exits_non_zero_when_wedged(beats, monkeypatch):
    alive.touch("worker.pass")
    assert alive.main(["120"]) == 0
    monkeypatch.setattr(alive, "newest_age_s", lambda now=None: 9999.0)
    assert alive.main(["120"]) == 1


def test_recording_liveness_never_raises(monkeypatch):
    """It runs in heartbeat's `finally`, where an exception would
    replace whatever the loop was actually reporting."""
    monkeypatch.setattr(alive, "BEAT_DIR", chr(0) + "//nope")
    alive.touch("worker.pass")
    alive.draining(True)


# -- the second blocker: the verifier's own tar and git work -------------
#
# The Kubernetes calls were only half of it. `verify_claim` shelled out to
# `git archive` once per mount and parsed whole tars INLINE, on a
# container capped at 1 CPU -- so verification stopped the admission
# sweep, the dispatch sweep and the heartbeats for as long as it ran.
# Measured on execution 203: 75.4s for two criteria, two conductors
# frozen for 154s and 184s, both restarted by the probe above.
#
# `kernel/workspace/deliver.py` had always wrapped its one `git rev-parse`
# in `to_thread`. `kernel/verifier/` never did, anywhere.

KERNEL = pathlib.Path(alive.__file__).resolve().parents[1]
VERIFIER = KERNEL / "verifier"

# THE GUARD WAS SCOPED TOO NARROWLY THE FIRST TIME. It covered
# `verifier/` and `drivers/`, so four synchronous `podfs_client` calls in
# `worker/` and `harness/` survived the audit -- including
# `envsetup._already_done`, which runs on EVERY leg. `podfs_client.call`
# is a `urlopen` with a 120s timeout, so one slow workspace pod froze
# every coroutine in the process. Widened here so the next one is caught
# by the build rather than by a production freeze.
WIDER = ("worker", "harness")

# Synchronous and slow, called from async code. `read_file` and the rest
# are podfs round trips; `web_fetch` is up to four 20s hops; the
# `tools_worker` entries shell out to git.
_OFF_LOOP = {"read_file", "archive", "archive_mounts", "commit", "publish",
             "heads", "digest", "explore", "plan", "credentials",
             "revoke_credentials", "from_pod", "from_dir",
             "read_workspace_file", "web_fetch", "story_commit"}

# Pure-Python tar surgery and git subprocesses. Every one is O(tree).
_BLOCKING = {"names", "slice_subtree", "splice", "only_new", "restamp",
             "archive_mounts", "archive_committed_tree", "archive_in_pod",
             "head_commit", "mount_digest", "mount_environment"}


def _offloaded(node) -> bool:
    return (isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Attribute)
                  and node.func.attr == "to_thread")
                 or (isinstance(node.func, ast.Name)
                     and node.func.id == "to_thread")))


def _inline_blocking(fn) -> list:
    """Blocking helpers called on the event loop inside one async def.

    Two subtrees are skipped because they do NOT run on the loop: the
    arguments of a `to_thread(...)` call, and the body of a nested
    function or lambda (which in this package exists to be handed to
    `to_thread`).
    """
    found, stack = [], list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if _offloaded(node) or isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _BLOCKING):
            found.append(f"{node.func.id} at line {node.lineno}")
        stack.extend(ast.iter_child_nodes(node))
    return found


def test_the_verifier_never_tars_on_the_event_loop():
    offenders = {}
    for path in sorted(VERIFIER.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef)]:
            bad = _inline_blocking(fn)
            if bad:
                offenders[f"{path.name}:{fn.name}"] = bad
    assert offenders == {}, (
        "these run git or whole-tar work directly on the event loop, so "
        "the conductor answers nothing while they run -- wrap them in "
        f"asyncio.to_thread: {offenders}")


def test_the_audit_would_notice_a_regression():
    """A pattern that matches nothing passes vacuously."""
    src = ("async def f():\n"
           "    x = names(tar)\n"
           "    y = await asyncio.to_thread(names, tar)\n")
    fn = ast.parse(src).body[0]
    found = _inline_blocking(fn)
    assert len(found) == 1 and found[0].startswith("names"), found


def test_worker_and_harness_do_not_block_the_loop_either():
    """R2. The first guard covered `verifier/` and `drivers/` only, and
    four synchronous podfs calls in `worker/` and `harness/` lived
    through it -- one of them on the path every leg takes."""
    offenders = {}
    for package in WIDER:
        for path in sorted((KERNEL / package).rglob("*.py")):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, ast.AsyncFunctionDef)]:
                bad = _inline_named(fn, _OFF_LOOP)
                if bad:
                    offenders[f"{package}/{path.name}:{fn.name}"] = bad
    assert offenders == {}, (
        "these call a synchronous podfs / git / http helper directly on "
        f"the event loop: {offenders}")


def _inline_named(fn, names: set) -> list:
    """Blocking helpers called on the loop, by bare name or attribute."""
    found, stack = [], list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if _offloaded(node) or isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            f = node.func
            name = (f.id if isinstance(f, ast.Name)
                    else f.attr if isinstance(f, ast.Attribute) else "")
            if name in names:
                found.append(f"{name} at line {node.lineno}")
        stack.extend(ast.iter_child_nodes(node))
    return found


def test_the_wider_audit_would_have_caught_the_real_bug():
    """Non-vacuity, against the exact shape that survived the first
    audit: `envsetup._already_done` calling podfs `read_file` inline."""
    src = ("async def _already_done(host, workspace, marker, execution_id):\n"
           "    _, problem = podfs_client.read_file(host, workspace, marker,\n"
           "                                        execution_id=execution_id)\n"
           "    return not problem\n")
    fn = ast.parse(src).body[0]
    assert _inline_named(fn, _OFF_LOOP), "the audit matches nothing"

    fixed = ("async def _already_done(host, workspace, marker, execution_id):\n"
             "    _, problem = await asyncio.to_thread(\n"
             "        podfs_client.read_file, host, workspace, marker,\n"
             "        execution_id=execution_id)\n"
             "    return not problem\n")
    assert not _inline_named(ast.parse(fixed).body[0], _OFF_LOOP)


# -- R3/R4: bounded loops, bounded legs, per-loop liveness ---------------

def test_verification_and_answering_are_bounded():
    """Each holds a pooled connection (POOL_MAX 16) for the whole of a
    container or a model call, and both loops were uncapped where the leg
    path has always been sliced. Twenty VERIFYING stories exhausted the
    pool and the next tick's acquire waited for ever."""
    from kernel.answerer.deliver import ANSWER_CONCURRENCY
    from kernel.verifier.adjudicator import verify_concurrency

    assert 0 < verify_concurrency() < 16
    assert 0 < ANSWER_CONCURRENCY < 16
    assert verify_concurrency() + ANSWER_CONCURRENCY < 16, (
        "together they must still leave the tick its own connections")


def test_acquiring_a_connection_is_bounded_and_named():
    """`pool.acquire()` waits for ever. The waiting was not the defect --
    the SILENCE was: no error, no heartbeat, and a liveness kill for a
    process that was patiently queueing."""
    from kernel.worker.service import ACQUIRE_MAX_S, PoolBusy, acquired

    assert 0 < ACQUIRE_MAX_S <= 60
    assert issubclass(PoolBusy, RuntimeError)
    assert acquired is not None


def test_a_hung_leg_gives_its_slot_back():
    """Nothing under a leg has its own timeout, so a socket that never
    answers held a slot and a connection for the life of the process."""
    from kernel.worker.supervisor import leg_max_s

    assert leg_max_s() > 615, (
        "must be past env_setup's own ceiling, or it disciplines slow "
        "work rather than reclaiming hung work")


def test_a_cancelled_leg_is_not_retried_into_the_breaker():
    from kernel.harness.models import retry

    src = pathlib.Path(retry.__file__).read_text(encoding="utf-8")
    cancel = src.index("except asyncio.CancelledError")
    broad = src.index("except BaseException")
    assert cancel < broad, (
        "CancelledError must be caught BEFORE the broad handler, or "
        "cancelling a leg calls the provider again and charges the "
        "circuit breaker for it")


def test_one_stopped_loop_is_caught_even_while_others_tick(beats):
    """THE PROBE'S BLIND SPOT. `worker.pass` every 4s masked a
    `reconcile_all` hung in a Kubernetes call -- which also holds the
    advisory lock, so the peer replica stopped reconciling too."""
    alive.touch("worker.reconcile", every=60.0)
    stale = time.time() - 3600
    os.utime(beats / "worker.reconcile", (stale, stale))
    alive.touch("worker.pass", every=4.0)

    healthy, why = alive.verdict(120.0)
    assert not healthy, why
    assert "worker.reconcile" in why
    assert "peer replica" in why


def test_a_slow_loop_is_judged_against_its_own_cadence(beats):
    """A 300s sweep must not be called dead at 120s just because a 4s
    loop exists beside it."""
    alive.touch("worker.credential_sweep", every=300.0)
    # 400s old: past the 120s floor, well inside 4 x 300s.
    stale_at = time.time() - 400
    os.utime(beats / "worker.credential_sweep", (stale_at, stale_at))
    alive.touch("worker.pass", every=4.0)
    healthy, why = alive.verdict(120.0)
    assert healthy, f"judged against the floor, not its own interval: {why}"

    # 4000s old: past 10 x 300s, so genuinely late. The factor is
    # deliberately conservative -- see _LATE_FACTOR; this number restarts
    # pods, and only the multiplier separates a long pass from a hung one.
    stale_at = time.time() - 4000
    os.utime(beats / "worker.credential_sweep", (stale_at, stale_at))
    healthy, why = alive.verdict(120.0)
    assert not healthy and "credential_sweep" in why


def test_a_file_without_an_interval_still_parses(beats):
    """Written by the previous build, read by this one, mid-rollout."""
    (beats / "worker.pass").write_text(str(time.time()))
    healthy, _ = alive.verdict(120.0)
    assert healthy


# -- catching the wedge, rather than watching for it ---------------------

def test_a_failing_probe_asks_for_a_stack_dump(beats, monkeypatch):
    """A wedge takes ~17 minutes to appear and ends when the kubelet
    kills the pod, so catching one by hand means sitting and watching --
    and by the time anyone looks the evidence has been restarted away.

    The probe is the only thing present at the right moment, and it is
    NOT the wedged process: the kubelet execs it fresh, which is exactly
    why it still runs when the event loop does not."""
    signalled = []
    monkeypatch.setattr(alive, "newest_age_s", lambda now=None: 9999.0)
    monkeypatch.setattr(alive, "dump_the_wedged_process",
                        lambda *a: signalled.append(True))
    assert alive.main(["120"]) == 1
    assert signalled == [True]


def test_a_healthy_probe_asks_for_nothing(beats, monkeypatch):
    signalled = []
    monkeypatch.setattr(alive, "dump_the_wedged_process",
                        lambda *a: signalled.append(True))
    alive.touch("worker.pass", every=4.0)
    assert alive.main(["120"]) == 0
    assert signalled == []


def test_the_dump_cannot_turn_an_instrument_into_an_outage(monkeypatch):
    """A probe that failed because it could not take a diagnostic would
    be worse than no diagnostic."""
    def _boom(pid):
        raise PermissionError("not permitted")

    monkeypatch.setattr(alive.os, "kill", _boom)
    alive.dump_the_wedged_process()          # must not raise
