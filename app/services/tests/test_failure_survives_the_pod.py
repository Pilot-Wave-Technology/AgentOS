"""When a pod dies, the reason has to outlive it.

WHAT THIS COST, exactly. `workspace-compose` exited 1 on five
consecutive dispatches of execution 6. Every one of them reached the
conductor log as:

    pod exec-6 reached Failed before becoming ready — it exited
    rather than started

The actual cause was one line in that container's log. Nobody could
read it: `await_ready` raising is what makes `dispatch_admitted` destroy
the pod, and `kubectl logs` against a deleted pod returns nothing.
Recovering it took a script polling the API server in a loop, racing our
own cleanup, and it lost the race twice.

TWO HALVES, AND BOTH ARE NEEDED. Kubernetes only copies a dead
container's log tail into `status` when the container asks it to
(`terminationMessagePolicy: FallbackToLogsOnError`; the default, `File`,
reports whatever was written to /dev/termination-log, which for a Python
process that died of an exception is nothing). And the driver has to
read it while the pod object is still in hand, because a moment later
there is no pod.
"""
from __future__ import annotations

from types import SimpleNamespace

from kernel.drivers.k8s_spec import pod_manifest
from kernel.drivers.kubernetes import _exit_detail
from kernel.spec import ExecutionSpec
from kernel.workspace.mounts import Mount


def _manifest() -> dict:
    spec = ExecutionSpec(image="img", story_id="s1", execution_id=1,
                         workspace_mounts=(
                             Mount(at="repos/a", binding="git",
                                   source="https://github.com/a/b", ref="",
                                   writable=True, kind="repo"),))
    return pod_manifest(spec, "exec-1", namespace="ns", pvc_name="ws-exec-1",
                        service_account="sa", bridge_image="img")


def _terminated(name, code, message, reason="Error"):
    return SimpleNamespace(
        name=name,
        state=SimpleNamespace(terminated=SimpleNamespace(
            exit_code=code, message=message, reason=reason)))


def _pod(init=(), run=()):
    return SimpleNamespace(status=SimpleNamespace(
        init_container_statuses=list(init), container_statuses=list(run)))


# ── the pod is asked to keep its own last words ──────────────────────

def test_every_container_falls_back_to_its_log():
    pod = _manifest()
    everything = (pod["spec"]["initContainers"] + pod["spec"]["containers"])
    assert everything, "no containers — the check would pass vacuously"
    for container in everything:
        assert container["terminationMessagePolicy"] == \
            "FallbackToLogsOnError", container["name"]


def test_the_init_containers_are_covered_too():
    # THE ONE THAT MATTERED. Both failures that hid behind the generic
    # message were init containers, and they are the containers a
    # builder is most likely to forget, because they are assembled in a
    # different function from the ones that run.
    names = [c["name"] for c in _manifest()["spec"]["initContainers"]]
    assert "workspace-compose" in names and "bridge-deliver" in names


# ── the driver carries it out before the pod is destroyed ────────────

def test_the_failed_container_is_named_with_what_it_said():
    detail = _exit_detail(_pod(init=[
        _terminated("workspace-compose", 1,
                    "Traceback (most recent call last):\n"
                    "  File \"/app/kernel/workspace/bindings.py\", line 180\n"
                    "fatal: could not read Username for 'https://github.com'")]))
    assert "workspace-compose" in detail
    assert "could not read Username" in detail


def test_only_the_tail_survives():
    # A full traceback would push the cause off the top of a log line.
    # The last line of a Python traceback is the exception; the middle
    # is frames a reader of a conductor log cannot act on.
    said = "\n".join(f"frame {i}" for i in range(20)) + "\nthe real cause"
    detail = _exit_detail(_pod(run=[_terminated("worker", 2, said)]))
    assert "the real cause" in detail
    assert "frame 0" not in detail


def test_a_container_that_exited_cleanly_is_not_reported():
    assert _exit_detail(_pod(init=[_terminated("bridge-deliver", 0, "")])) == ""


def test_a_missing_message_says_so_rather_than_nothing():
    # Silence here is indistinguishable from "it said nothing useful",
    # and the difference is whether the policy above is in effect --
    # which is precisely the thing that was wrong.
    detail = _exit_detail(_pod(run=[_terminated("worker", 1, "")]))
    assert "worker exited 1" in detail
    assert "terminationMessagePolicy" in detail


def test_a_healthy_pod_adds_nothing():
    assert _exit_detail(_pod()) == ""
