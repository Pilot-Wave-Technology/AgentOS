"""D2's four inherited items, each with the assertion that pins it.

11.5i handed D2 two things and the brief added two more. They have
nothing in common except that each is a fact somebody already worked out
and wrote down, which a later change could quietly falsify:

  * THE POD'S FAN-OUT. `pool_safe_fanout()` derived the most legs a
    process may run from the CONDUCTOR's 16-connection pool, so it
    answered 10 for a pod whose pool tops out at TWO. Four `advance_one`
    holders over two connections is the wedge, and the fourth waits on a
    connection that is not coming back until a leg ends, minutes away.

  * `off` AS THE ONE SWITCH. `k8s_sqlproxy.OFF` said it existed because
    "D2 removes the need for it"; 11.5a prerequisite 3 said it was
    load-bearing. Not a disagreement about a fact -- opposite intentions
    -- and D2 decided for load-bearing.

  * TWO CITATIONS D2 WAS TOLD TO CHECK RATHER THAN REPOINT, because the
    paragraph in `loopbridge` is an ARGUMENT for why pool sizing is not a
    `make_pool(dsn, size=...)` parameter, and D2 is exactly the change
    that could have made one of its two callers pass a size.

  * I10's MODEL-KEY HALF: "after D the model key is in the worker and
    `bash` still cannot read it."

A manifest is data and a clamp is arithmetic, so none of this needs a
cluster. None of it shows a pod running a leg either -- nothing starts
`leg_runner` in one yet and `legs_run_in_the_pod()` is False in every
environment that exists. These are the checks that make the cutover safe
to attempt, not evidence that it happened.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


@contextlib.contextmanager
def _in_the_pod(yes: bool = True):
    """Presence of `AGENTOS_LEG_DB_USER` is how a process knows it is one."""
    was = os.environ.pop("AGENTOS_LEG_DB_USER", None)
    if yes:
        os.environ["AGENTOS_LEG_DB_USER"] = "agentos_leg_test"
    try:
        yield
    finally:
        os.environ.pop("AGENTOS_LEG_DB_USER", None)
        if was is not None:
            os.environ["AGENTOS_LEG_DB_USER"] = was


# -- the pod's fan-out: the wedge D2 was told to look for ----------------

def test_the_clamp_answers_for_the_pool_this_process_actually_has():
    from kernel.loopbridge import LEG_POOL_MAX, POOL_MAX
    from kernel.worker.concurrency import pool_safe_fanout

    assert POOL_MAX == 16 and LEG_POOL_MAX == 2
    assert pool_safe_fanout() == 10
    with _in_the_pod():
        assert pool_safe_fanout() == 1, (
            "the pod would fan legs out over the CONDUCTOR's pool")


def test_even_the_default_is_clamped_in_the_pod():
    """It was not. Only a typed value was checked, and the variable is
    unset everywhere -- so the pod would have taken four `advance_one`
    holders over a two-connection pool with nobody having configured
    anything at all."""
    from kernel.worker.concurrency import (DEFAULT_LEG_CONCURRENCY,
                                           LEG_CONCURRENCY_ENV,
                                           leg_concurrency)

    assert LEG_CONCURRENCY_ENV not in os.environ
    assert leg_concurrency() == DEFAULT_LEG_CONCURRENCY == 4
    with _in_the_pod():
        assert leg_concurrency() == 1


def test_the_fanout_never_reaches_zero():
    """A reservation bigger than the pool is a process that admits work
    and then runs none of it, silently."""
    from kernel.worker.concurrency import pool_safe_fanout

    with _in_the_pod():
        assert pool_safe_fanout() >= 1


# -- one variable decides where legs run ---------------------------------

def test_off_is_the_switch_and_there_is_only_one_of_it():
    """Two variables saying this is the worst failure available: either
    nobody claims a leg and the fleet is silently idle, or both do and
    two models run tool calls against one workspace."""
    from kernel.drivers import k8s_sqlproxy
    from kernel.worker.executor_pass import (conductor_leg_slots,
                                             legs_run_in_the_pod)

    env = k8s_sqlproxy.INSTANCE_ENV
    was = os.environ.pop(env, None)
    try:
        assert legs_run_in_the_pod() is False
        assert conductor_leg_slots() == 4

        os.environ[env] = k8s_sqlproxy.OFF
        assert legs_run_in_the_pod() is False, "`off` must mean off"
        assert conductor_leg_slots() == 4

        os.environ[env] = "proj:us-central1:db"
        assert legs_run_in_the_pod() is True
        assert conductor_leg_slots() == 0
    finally:
        os.environ.pop(env, None)
        if was is not None:
            os.environ[env] = was


def test_off_stayed_load_bearing_and_both_texts_now_say_so():
    """`k8s_sqlproxy` said `off` existed because 'D2 removes the need for
    it'; 11.5a prerequisite 3 said it was load-bearing. D2 decided, and a
    decision that lives in one of two contradicting comments is not one."""
    proxy = _src("kernel/drivers/k8s_sqlproxy.py")
    assert "`off` is PERMANENT" in proxy
    # The retired sentence survives ONLY as a quotation of what it used
    # to say. A comment that is simply deleted leaves the next reader to
    # rediscover the contradiction; one that is overwritten in place
    # leaves them unable to tell a decision from a typo.
    assert 'USED TO END "D2 removes the need for it"' in proxy
    assert proxy.count("D2 removes the need for it") == 1


# -- the two citations D2 was told to CHECK, not merely repoint ----------

def test_both_make_pool_callers_still_hand_it_a_bare_dsn():
    """The paragraph in `loopbridge` is an ARGUMENT, not a pointer: pool
    sizing is derived from the process, so a `size=` here would be a
    SECOND way to say 'I am the pod' and the two could disagree. D2 is
    exactly the change that could have broken it."""
    for rel in ("kernel/worker/service_pool.py",
                "kernel/harness/leg_runner.py"):
        tree = ast.parse(_src(rel))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "make_pool"]
        assert calls, f"{rel} no longer calls make_pool"
        for call in calls:
            assert not call.keywords, f"{rel} now declares a pool size"
            assert len(call.args) == 1


def test_the_repointed_citations_resolve():
    """11.5f's lesson: an anchor pinned to a path its subject has left
    matches nothing and proves nothing."""
    from kernel.harness import executor_worker_dispatch
    from kernel.worker import service_pool

    assert callable(executor_worker_dispatch.worker_execute)
    assert callable(service_pool.wait_for_pool)
    assert "executor_worker_dispatch.py::worker_execute" in _src(
        "kernel/workspace/deliver.py")
    assert "service_pool.py::wait_for_pool" in _src("kernel/loopbridge.py")


# -- I10, the half D2 can prove from a manifest --------------------------

def test_the_model_key_would_reach_the_worker_and_not_the_sandbox():
    """I10 after D: 'the model key is in the worker and bash still cannot
    read it.' The key is not in the pod yet -- POD_ENV_ALLOWLIST is a
    closed list and carries none -- so what is provable today is the
    BOUNDARY itself: anything the worker is given, the sandbox is not."""
    from kernel.drivers.k8s_spec import SANDBOX, WORKER, pod_manifest
    from kernel.spec import ExecutionSpec

    spec = ExecutionSpec(image="agentos:1", execution_id=7,
                         env={"ANTHROPIC_API_KEY": "sk-not-a-real-key"})
    pod = pod_manifest(spec, "exec-7", namespace="ns", pvc_name="ws-7",
                       service_account="agentos-execution",
                       bridge_image="registry/kernel:test")
    by_name = {c["name"]: c for c in pod["spec"]["containers"]}
    worker_env = {e["name"] for e in by_name[WORKER]["env"]}
    sandbox_env = {e["name"] for e in by_name[SANDBOX]["env"]}

    assert "ANTHROPIC_API_KEY" in worker_env
    assert "ANTHROPIC_API_KEY" not in sandbox_env
    assert pod["spec"]["shareProcessNamespace"] is False, (
        "the sandbox could read the worker's key out of /proc")


def test_the_allowlist_carries_no_model_key_yet_and_that_is_the_gap():
    """Written as a test so the day somebody adds one is the day this
    fails and they read I10, rather than discovering it in a pod."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    assert not [n for n in POD_ENV_ALLOWLIST
                if "API_KEY" in n or "TOKEN" in n or "SECRET" in n]
