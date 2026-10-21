"""A sandbox in a pod can be reached, and only by the conductor.

THE DEFECT. The Kubernetes execution pod ran `["sleep", "infinity"]` in
its sandbox, so the bridge shim never started. Even had it started,
`call_bridge` built `http://localhost:{port}` with no host parameter at
all -- and on Kubernetes the conductor is a DIFFERENT POD, so every tool
call would have gone to the conductor's own loopback.

Fixing the address costs the control that came with it: the shim binds
127.0.0.1 precisely so that under a shared netns nothing else can call
it. A pod shares no netns with its caller, so the bind widens to 0.0.0.0
and an Ingress NetworkPolicy becomes the only thing in front of a
service that runs bash.
"""
import io
import pathlib
import re

import pytest

from kernel.bridge.client import BRIDGE_HOST
from kernel.bridge.shim import DEFAULT_BIND, _bind_from
from kernel.conductor.spec_builder import build_spec
from kernel.drivers import k8s_bridge
from kernel.drivers.k8s_spec import pod_manifest

ROOT = pathlib.Path(__file__).resolve().parents[3]
ROW = {"id": 1, "kind": "errand", "domain": "ops", "goal_id": "G"}


def _pod(monkeypatch=None):
    # The image is passed, not read from the environment: `pod_manifest`
    # is a pure function of its arguments and the driver is where the
    # deployment fact is resolved and refused.
    spec = build_spec(ROW, [], {})
    return pod_manifest(spec, "exec-1", namespace="ns",
                        pvc_name="ws-exec-1", service_account="sa",
                        bridge_image="registry/kernel:abc")


# ── the shim listens where it is told ────────────────────────────────

def test_the_shim_still_defaults_to_loopback():
    # Docker and E0 share a netns with the sandbox; 127.0.0.1 is both
    # correct and the tightest reach there is. Widening the default
    # would weaken every existing deployment to serve one new one.
    assert DEFAULT_BIND == "127.0.0.1"
    assert _bind_from([]) == "127.0.0.1"


def test_a_pod_sandbox_is_told_to_bind_widely():
    assert _bind_from(["--bind", "0.0.0.0"]) == "0.0.0.0"


def test_a_dangling_bind_flag_falls_back_rather_than_crashing():
    assert _bind_from(["--bind"]) == DEFAULT_BIND


def test_the_bind_is_an_argument_not_an_environment_variable(monkeypatch):
    # The sandbox is created with `"env": []` -- not "no secrets" but
    # none at all. A bind address is not a credential, but putting it in
    # the env would be the first exception to a rule whose whole value
    # is that it has none.
    monkeypatch.setenv("AGENTOS_BRIDGE_BIND", "0.0.0.0")
    assert _bind_from([]) == "127.0.0.1"


# ── the caller can say where ─────────────────────────────────────────

def test_localhost_is_still_the_default_host():
    assert BRIDGE_HOST == "localhost"


def test_call_bridge_takes_a_host():
    # The defect was that it did not: the URL was a literal localhost,
    # so no caller could reach a sandbox in another pod.
    import inspect

    from kernel.bridge.client import bridge_health, call_bridge

    assert "host" in inspect.signature(call_bridge).parameters
    assert "host" in inspect.signature(bridge_health).parameters


def test_the_url_is_built_from_the_host_not_a_literal():
    src = io.open(ROOT / "kernel/bridge/client.py", encoding="utf-8").read()
    # A literal `http://localhost:` left anywhere in this file is the
    # defect returning by a different route.
    assert "http://localhost:" not in src


@pytest.mark.asyncio
async def test_a_driver_that_shares_a_namespace_answers_localhost():
    from kernel.drivers.docker import DockerDriver

    assert await DockerDriver.resolve_bridge_host(None, "any") == "localhost"


# ── the pod delivers and runs the shim ───────────────────────────────

def test_the_sandbox_no_longer_sleeps(monkeypatch):
    pod = _pod(monkeypatch)
    sandbox = [c for c in pod["spec"]["containers"] if c["name"] == "sandbox"][0]
    assert sandbox["command"] != ["sleep", "infinity"]
    assert "shim.py" in " ".join(sandbox["command"])
    assert "--bind" in sandbox["command"]


def test_the_bridge_is_delivered_by_an_init_container(monkeypatch):
    # An initContainer and not a sidecar: a pod whose bridge could not be
    # copied must never become Ready, rather than starting a sandbox that
    # answers nothing and failing later as a tool-call timeout.
    pod = _pod(monkeypatch)
    names = [c["name"] for c in pod["spec"]["initContainers"]]
    assert "bridge-deliver" in names


def test_the_sandbox_cannot_edit_the_shim(monkeypatch):
    # Docker's bind mount is `:ro` because "a sandbox able to edit the
    # shim could rewrite" it. The pod must keep that guarantee.
    pod = _pod(monkeypatch)
    sandbox = [c for c in pod["spec"]["containers"] if c["name"] == "sandbox"][0]
    bridge = [m for m in sandbox["volumeMounts"]
              if m["mountPath"] == k8s_bridge.BRIDGE_DIR]
    assert bridge and bridge[0].get("readOnly") is True


def test_the_shim_does_not_live_in_the_writable_workspace():
    # Under /workspace it would be writable by the sandbox, which is the
    # same hole the read-only mount exists to close.
    assert not k8s_bridge.BRIDGE_DIR.startswith("/workspace")


def test_no_image_carrying_the_shim_is_refused_by_name(monkeypatch):
    monkeypatch.delenv("AGENTOS_EXECUTION_IMAGE", raising=False)
    with pytest.raises(k8s_bridge.BridgeImageMissing) as e:
        k8s_bridge.kernel_image()
    assert "AGENTOS_EXECUTION_IMAGE" in str(e.value)


def test_the_init_container_is_in_autopilot_ratio():
    gib = k8s_bridge.INIT_MEMORY_MB / 1024
    assert 1.0 <= gib / float(k8s_bridge.INIT_CPUS) <= 6.5


# ── and only the conductor may call it ───────────────────────────────

def test_an_ingress_policy_fronts_the_bridge_port():
    # With the bind widened to 0.0.0.0 this is not defence in depth. The
    # namespace has no other ingress policy, which in Kubernetes means
    # every pod may reach every port -- so this is the ONLY control in
    # front of a service that runs bash.
    manifest = io.open(ROOT / "deploy/k8s/conductor.yaml",
                       encoding="utf-8").read()
    assert "execution-bridge-ingress" in manifest
    block = manifest[manifest.index("execution-bridge-ingress"):]
    block = block[:block.index("\n---")]
    assert "policyTypes: [Ingress]" in block
    assert "app: agentos-execution" in block   # it selects execution pods
    assert "app: conductor" in block           # from the conductor only
    assert re.search(r"port:\s*9100", block)


def test_the_policy_selects_the_label_the_pod_actually_carries(monkeypatch):
    # A selector that matches nothing is a policy that protects nothing,
    # and it fails open rather than closed.
    pod = _pod(monkeypatch)
    assert pod["metadata"]["labels"]["app"] == "agentos-execution"


# ── the shared dispatch call site ────────────────────────────────────

def test_every_driver_accepts_what_dispatch_actually_passes():
    """`dispatch_admitted` calls await_ready(cid, bridge_port=...).

    THE FAILURE THIS GUARDS. KubernetesDriver.await_ready took only
    (container_id, timeout), so the call raised
    `TypeError: unexpected keyword argument 'bridge_port'` -- AFTER the
    pod was created. Dispatch's cleanup then destroyed the pod, the
    claim survived (as it must, for preemption), and the next tick built
    it again: three pods created and destroyed per minute, orphaned
    claims against a quota of 40, and the run stuck in DISPATCHING.

    A shared call site cannot carry an argument only some
    implementations accept, so this asserts the signature rather than
    the behaviour -- a driver is free to ignore the value, and the pod
    one does, because a pod has exactly one sandbox on a fixed port.
    """
    import inspect

    from kernel.drivers.docker import DockerDriver
    from kernel.drivers.kubernetes import KubernetesDriver

    for driver in (DockerDriver, KubernetesDriver):
        params = inspect.signature(driver.await_ready).parameters
        assert "bridge_port" in params, f"{driver.__name__} refuses it"


def test_the_pod_is_given_time_to_clone_before_it_is_called_unready():
    # Two init containers now run first, one of them a git clone. Thirty
    # seconds was right for a pod that only had to pull an image.
    import inspect

    from kernel.drivers.kubernetes import KubernetesDriver

    timeout = inspect.signature(
        KubernetesDriver.await_ready).parameters["timeout"].default
    assert timeout >= 120


def test_a_pod_held_by_an_init_container_says_which_one():
    # "phase=Pending" does not distinguish "still cloning" from "the
    # clone failed", and those have different fixes.
    from kernel.drivers.kubernetes import _init_detail

    class _S:
        def __init__(self, name, reason):
            self.name, self.ready = name, False
            self.state = type("st", (), {
                "waiting": type("w", (), {"reason": reason})(),
                "terminated": None})()

    class _Pod:
        status = type("p", (), {"init_container_statuses": [
            _S("workspace-compose", "CrashLoopBackOff")]})()

    assert "workspace-compose" in _init_detail(_Pod())
    assert "CrashLoopBackOff" in _init_detail(_Pod())


# ── both containers must be able to run, and to write ────────────────

def test_the_worker_has_a_uid_of_its_own(monkeypatch):
    """Found on the cluster: the pod reached 1/2 and stopped.

        container has runAsNonRoot and image will run as root
        (container: worker)

    `runAsNonRoot: True` was set for both containers but `runAsUser`
    only ever for the SANDBOX -- the worker was left to its image's own
    user. Survivable while it ran a stock image and merely slept;
    fatal the moment it ran the kernel's image, which declares no USER.

    The symptom is the misleading part: a pod at 1/2 with the sandbox
    running fine reads as a sandbox problem.
    """
    from kernel.drivers.k8s_spec import WORKER_UID

    pod = _pod(monkeypatch)
    worker = [c for c in pod["spec"]["containers"] if c["name"] == "worker"][0]
    assert worker["securityContext"]["runAsUser"] == WORKER_UID
    assert worker["securityContext"]["runAsNonRoot"] is True


def test_the_two_containers_still_run_as_different_users(monkeypatch):
    # The property the original code was protecting: the sandbox must
    # not be able to overwrite the worker's files.
    pod = _pod(monkeypatch)
    uids = {c["name"]: c["securityContext"]["runAsUser"]
            for c in pod["spec"]["containers"]}
    assert uids["worker"] != uids["sandbox"]


def test_they_share_a_group_because_they_share_a_tree(monkeypatch):
    # Two uids with no common group means one of them gets EACCES on a
    # workspace it half owns -- the model failing at its first edit, or
    # a commit that finds nothing staged.
    from kernel.drivers.k8s_spec import WORKSPACE_GID

    pod = _pod(monkeypatch)
    assert pod["spec"]["securityContext"]["fsGroup"] == WORKSPACE_GID


def test_the_composer_writes_as_the_worker_not_as_root():
    # A tree composed as root is one neither container can write.
    # Needs a spec WITH mounts: a run with no grants gets no composer.
    from kernel.drivers.k8s_spec import WORKER_UID
    from kernel.workspace.mounts import Mount

    mount = Mount(at="repos/o__r", binding="git", source="https://x/r.git",
                  ref="feat/b", writable=True, kind="branch")
    spec = build_spec(ROW, [], {}, mounts=(mount,))
    pod = pod_manifest(spec, "exec-1", namespace="ns", pvc_name="ws-exec-1",
                       service_account="sa", bridge_image="registry/k:abc")
    init = {c["name"]: c for c in pod["spec"]["initContainers"]}
    assert init["workspace-compose"]["securityContext"]["runAsUser"] == WORKER_UID


def test_no_container_is_allowed_to_run_as_root(monkeypatch):
    pod = _pod(monkeypatch)
    for c in pod["spec"]["initContainers"] + pod["spec"]["containers"]:
        ctx = c.get("securityContext") or {}
        assert ctx.get("runAsUser", 0) != 0, f"{c['name']} runs as root"


def test_the_bridge_copy_does_not_try_to_preserve_ownership(monkeypatch):
    """`cp -a` cost a pod, and the failure looked like nothing.

    `-a` implies `-p`: preserve mode, ownership AND timestamps. Only
    root may do the last two on files it creates, so once this container
    ran as a non-root uid -- which it must, `runAsNonRoot` is set and
    the kernel image declares no USER -- the copy moved every byte and
    then exited non-zero on

        cp: preserving times for '/opt/agentos-bridge/.':
        Operation not permitted

    The pod reported Init:Error with a complete, usable bridge sitting
    in the volume. Nothing here needs preserving: the shim is read, not
    executed, and its owner MUST change for the sandbox to read it.
    """
    pod = _pod(monkeypatch)
    init = [c for c in pod["spec"]["initContainers"]
            if c["name"] == "bridge-deliver"][0]
    command = " ".join(init["command"])
    assert "cp -a" not in command, "a non-root copy cannot preserve"
    assert "cp -r" in command
