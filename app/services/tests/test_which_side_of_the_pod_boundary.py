"""Which SIDE of the pod boundary this process is on.

FOUND BY ASKING "what else could stall a leg", after execution 214 died
on `ensure_reachable` calling the Kubernetes API from inside a pod bound
to no Role. The answer was that 214 was not one bug. It was one WRONG
PREDICATE with several faces, and fixing only the face that had already
fired would have moved the failure four lines down the same function.

`is_pod(driver)` answers "is this execution's tree in a pod". Nine call
sites use it to decide whether to CROSS -- to ask the cluster where the
pod is and then speak podfs to it. Those are different questions, and
they agreed for as long as every caller was outside the pod. The cutover
put a caller INSIDE it, where they invert: the tree is in a pod, and
crossing to it is exactly wrong, because the PVC is mounted in this very
container.

THE THREE THAT WERE LIVE, all on one leg's path:

    reachability.ensure_reachable   fixed earlier; injected probe
    loop._take_leg                  `resolve_bridge_host` -> 403, leg dead
    harness.planfiles.persist_plan  swallowed at WARNING, returns []

THE LAST ONE IS THE WORST. It degrades quietly on purpose -- "a courtesy
copy must not block an approval" -- so it does not stall anything. It
just means a story's plan file is silently never written, which is the
vacuous-success shape this project keeps paying for.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]

MARKER = "AGENTOS_EXECUTION_ID"


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _code(rel: str) -> str:
    """Source with comments and docstrings stripped.

    FOUR TIMES IN ONE DAY an absence assertion in this suite matched the
    comment EXPLAINING the absence. A test that reads its own subject's
    prose is a test that fails when somebody documents the fix.
    """
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:]
    return ast.unparse(tree)


# ── the predicate itself ───────────────────────────────────────────────

def test_the_pod_knows_it_is_the_pod(monkeypatch):
    from kernel.workspace.podfs_client import in_execution_pod

    monkeypatch.delenv(MARKER, raising=False)
    assert in_execution_pod() is False

    monkeypatch.setenv(MARKER, "214")
    assert in_execution_pod() is True


def test_the_marker_is_one_the_conductor_cannot_have():
    """The whole predicate rests on this. `k8s_spec` puts it on the
    worker container; if it ever reached the conductor's Deployment the
    conductor would start reading pod trees off its own empty
    filesystem -- the exact failure `workspace_host` exists to prevent,
    with no error anywhere."""
    from kernel.workspace.podfs_client import EXECUTION_POD_ENV

    assert EXECUTION_POD_ENV == MARKER
    assert MARKER in _src("kernel/drivers/k8s_spec.py"), (
        "the pod manifest no longer sets the marker every crossing "
        "decision in the kernel now depends on")


def test_the_leg_in_the_pod_never_crosses(monkeypatch):
    """THE INVARIANT. Inside the pod the answer is False for EVERY
    driver, including the Kubernetes one -- that is the inversion."""
    from kernel.workspace import podfs_client

    class K8s:
        WORKSPACE_MODEL = "pod"

    monkeypatch.setenv(MARKER, "214")
    assert podfs_client.tree_is_remote(K8s()) is False, (
        "a leg inside the execution pod still thinks it must cross to "
        "its own filesystem")


def test_the_conductor_still_crosses(monkeypatch):
    """The other direction, and it matters as much. A conductor that
    stopped crossing would read an empty directory in its own container
    and report it as an empty RESULT."""
    from kernel.workspace import podfs_client

    class K8s:
        WORKSPACE_MODEL = "pod"

    class Docker:
        WORKSPACE_MODEL = "local"

    monkeypatch.delenv(MARKER, raising=False)
    assert podfs_client.tree_is_remote(K8s()) is True
    assert podfs_client.tree_is_remote(Docker()) is False


# ── loop._take_leg, which is where 214 would have died next ────────────

def test_the_leg_does_not_ask_the_cluster_where_it_is():
    """`_take_leg` called `driver.resolve_bridge_host` on EVERY leg. In
    the pod that is `read_namespaced_pod` against an API the egress proxy
    refuses with `Tunnel connection failed: 403`, for an identity bound
    to no Role. Same call, same 403, four lines after the one already
    fixed."""
    code = _code("kernel/worker/loop.py")

    assert "resolve_bridge_host" not in code, (
        "worker/loop.py resolves the bridge host itself again; in the "
        "pod that is a Kubernetes API call the leg cannot make")


def test_locate_answers_loopback_and_a_local_tree_in_the_pod(monkeypatch):
    from kernel.worker.reachability import LOOPBACK, locate

    monkeypatch.setenv(MARKER, "214")

    class Exploding:
        async def resolve_bridge_host(self, cid):
            raise AssertionError(
                "the leg asked the cluster where its own pod is")

    # The row as the pod reads it from the database: promotion wrote the
    # pod's IP into `workspace_host`, correctly, for the conductor.
    row = {"id": 214, "container_id": "exec-214",
           "workspace_host": "10.4.2.11"}
    asyncio.run(locate(Exploding(), row))

    assert row["bridge_host"] == LOOPBACK
    assert row["workspace_host"] == "", (
        "the leg kept the pod IP the conductor wrote, so every workspace "
        "read is an HTTP round trip out of the pod and back -- to a tree "
        "mounted in this very container")


def test_locate_is_unchanged_for_the_conductor(monkeypatch):
    from kernel.worker.reachability import locate

    monkeypatch.delenv(MARKER, raising=False)

    class K8s:
        WORKSPACE_MODEL = "pod"

        async def resolve_bridge_host(self, cid):
            return "10.4.2.11"

    row = {"id": 214, "container_id": "exec-214"}
    asyncio.run(locate(K8s(), row))

    assert row["bridge_host"] == "10.4.2.11"
    assert row["workspace_host"] == "10.4.2.11", (
        "the conductor stopped recording where the tree is, so it will "
        "read its own filesystem and report an empty workspace")


# ── the silent one ─────────────────────────────────────────────────────

def test_the_plan_file_is_written_where_the_leg_runs():
    """`persist_plan` swallows `WorkspaceUnreachable` at WARNING and
    returns [] -- correct for a courtesy copy, and it means the 403 was
    never going to appear as a failure. The story just has no plan."""
    code = _code("kernel/harness/planfiles.py")

    assert "tree_is_remote" in code
    assert "is_pod" not in code, (
        "persist_plan decides on `is_pod` again, so a leg inside the pod "
        "crosses to its own tree and loses the plan file silently")


def test_no_leg_side_module_decides_on_is_pod():
    """THE SWEEP, as a standing check. These are the modules a leg
    executes; `is_pod` in any of them is the 214 bug returning.

    The conductor's and the verifier's own modules are deliberately not
    listed -- they are outside the pod by construction, where `is_pod`
    and "must I cross" agree and the call is correct.
    """
    leg_side = ("kernel/worker/loop.py",
                "kernel/worker/advance.py",
                "kernel/worker/pod_loop.py",
                "kernel/harness/planfiles.py",
                "kernel/credentials/issue.py")

    for rel in leg_side:
        assert "is_pod" not in _code(rel), (
            f"{rel} runs in the leg and decides on `is_pod`, which is "
            f"true inside the pod and means the opposite of what the "
            f"call site wants")


def test_the_verifier_is_untouched_by_any_of_this():
    """Asked for by name. The verifier runs in its OWN Deployment,
    outside every execution pod, so it must keep crossing -- and it must
    not acquire the marker or the new predicate by accident."""
    for rel in ("kernel/verifier/service.py", "kernel/verifier/rebuild.py",
                "kernel/verifier/tree.py", "kernel/verifier/preempted.py"):
        code = _code(rel)
        assert "in_execution_pod" not in code, (
            f"{rel} asks whether it is inside an execution pod; it never "
            f"is, and a False answer there would stop it crossing")
        assert "locate(" not in code

    # And the ones that cross still do.
    for rel in ("kernel/verifier/rebuild.py", "kernel/verifier/tree.py",
                "kernel/verifier/preempted.py"):
        assert "is_pod" in _code(rel), (
            f"{rel} stopped deciding whether to cross; the verifier is "
            f"always outside the pod and always must")

def test_the_pods_bridge_host_is_the_sentinel_and_not_a_bare_address():
    """MEASURED ON EXECUTION 215, and it is the fourth face of this bug.

    `locate` first set the pod's bridge host to the literal
    "127.0.0.1". That is the correct ADDRESS and the wrong SENTINEL:
    several call sites decide whether they must cross by comparing
    against `BRIDGE_HOST` exactly --

        envsetup.py    remote = bool(host) and host != BRIDGE_HOST
        envrecord.py   not host or host == BRIDGE_HOST

    -- so "127.0.0.1" means "localhost" to the network and "somewhere
    else" to the comparison. Env setup concluded it had to reach the
    tree over podfs, tried to derive a workspace token, and died with
    `AGENT_TOKEN_SIGNING_KEY is unset`. The leg carried on, because a
    failed setup is not fatal, and the story's dependencies were
    silently never installed.

    WHY THE `is_pod` SWEEP MISSED IT. Here the crossing question is not
    a predicate at all -- it is a string comparison -- so nothing that
    grepped for `is_pod` or `tree_is_remote` could have found it. This
    check is pinned to the identity rather than to the value, so it
    keeps holding if the sentinel itself ever changes.
    """
    from kernel.bridge.client import BRIDGE_HOST
    from kernel.worker.reachability import LOOPBACK

    assert LOOPBACK == BRIDGE_HOST, (
        f"the pod reports its bridge as {LOOPBACK!r} while every "
        f"crossing decision compares against {BRIDGE_HOST!r}, so env "
        f"setup and env recording will both try to cross to a tree "
        f"mounted in this container")


def test_env_setup_does_not_cross_from_inside_the_pod(monkeypatch):
    """The behaviour the sentinel buys, checked at the call site's own
    expression rather than trusting that the constant is enough."""
    from kernel.bridge.client import BRIDGE_HOST
    from kernel.worker.reachability import LOOPBACK, locate

    monkeypatch.setenv(MARKER, "215")

    class Exploding:
        async def resolve_bridge_host(self, cid):
            raise AssertionError("asked the cluster")

    row = {"id": 215, "container_id": "exec-215",
           "workspace_host": "10.4.2.11"}
    asyncio.run(locate(Exploding(), row))

    # `envsetup.setups_for`'s own line, evaluated against what the pod
    # will actually be handed.
    host = row["bridge_host"]
    remote = bool(host) and host != BRIDGE_HOST

    assert remote is False, (
        "env setup still thinks the tree is remote, so the package "
        "install a story depends on never runs")
    assert LOOPBACK == BRIDGE_HOST

def _call_line(rel: str, func: str, call: str) -> int:
    """Line of the first call to `call` inside `func`. -1 if absent."""
    for node in ast.walk(ast.parse(_src(rel))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            fn = inner.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else "")
            if name == call:
                return inner.lineno
    return -1


def test_the_tree_is_located_before_the_setup_that_reads_it():
    """MEASURED TWICE -- executions 215 AND 216, the second after a fix
    that changed something else.

    `locate` lived only in `_take_leg`, which `_under_the_lock` reaches
    through `leg_body` -- twenty lines BELOW `ensure_setup`. So setup ran
    against the row as the database handed it over, `workspace_host`
    still carrying the pod IP `promote_row` wrote. Its own line is

        host = row.get("workspace_host") or BRIDGE_HOST

    and a non-empty value means CROSS, so the pod tried to reach its own
    tree over podfs and died on `AGENT_TOKEN_SIGNING_KEY is unset`. Never
    fatal, so the only symptom was a story whose dependencies were never
    installed.

    AN ORDERING BUG, WHICH IS WHY NOTHING CAUGHT IT. Every unit test of
    `locate` passed -- it does the right thing. Every unit test of
    `ensure_setup` passed. What was wrong was WHEN, and only a check
    shaped like this one can see that.
    """
    at_locate = _call_line("kernel/worker/advance.py", "_under_the_lock",
                           "locate")
    at_setup = _call_line("kernel/worker/advance.py", "_under_the_lock",
                          "ensure_setup")

    assert at_locate > 0, (
        "`_under_the_lock` no longer locates the tree before it sets the "
        "environment up, so env setup reads the pod IP the conductor "
        "wrote and crosses to a tree in its own container")
    assert at_setup > 0, "re-point this check; ensure_setup moved"
    assert at_locate < at_setup, (
        f"locate is at line {at_locate} and ensure_setup at {at_setup} -- "
        f"setup runs against a stale workspace_host, which is executions "
        f"215 and 216 exactly")


def test_setup_reads_the_field_this_ordering_is_about():
    """The other half: if `ensure_setup` ever stops reading
    `workspace_host`, the ordering check above is guarding nothing and
    should be re-pointed rather than left to pass on no evidence."""
    code = _code("kernel/worker/envsetup.py")

    assert "workspace_host" in code, (
        "ensure_setup no longer derives its host from workspace_host; "
        "the ordering guard above now proves nothing")
