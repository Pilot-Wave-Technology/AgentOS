"""An artifact the platform catalogues is one that can be read back.

WHAT WAS TRUE BEFORE. `emit_artifact` runs in the leg, and since the
leg-in-pod cutover the leg runs inside the execution pod
(`kernel/worker/pod_main.py`: "the workspace surface AND the leg loop").
That pod is handed ARTIFACT_STORE=filesystem rooted on its own
workspace, deliberately: it is denied the metadata server, because its
sandbox shares the network namespace and a credential there is one a
model can mint.

So the put SUCCEEDED and was useless. A real content hash, bytes on a
PVC, and a goal resource catalogued as durable, content-addressed and
mountable into later runs. Then the pod ended.

Measured: the artifact bucket took nothing between 2026-09-18T17:26 and
the day this was written, while five artifacts across four stories were
registered as though it had. Nobody noticed because an artifact CHECK
reads `kernel.artifact_manifest` -- it asks whether something was
emitted, never whether it can be read. Three stories reported "satisfied
by 1 artifact(s)" for objects that did not exist. It surfaced only when
a later story was granted one and its workspace build failed.

THE SEAM, IN BOTH DIRECTIONS. The pod holds no cloud credential, so the
control panel does the storage work at both ends:

    in    GET  /internal/resource-object   a granted document's bytes
    out   POST /internal/artifact          an emitted artifact's bytes

WHAT THIS PINS:

  * the write goes through the broker wherever there is one
  * a pod with no broker REFUSES rather than registering a lie
  * E0, where the filesystem store is genuinely durable, is unchanged
  * the worker can reach the endpoint and the SANDBOX cannot
"""
from __future__ import annotations

import inspect

import pytest

from kernel.harness import emit


def _worker_and_sandbox_env(control_panel_url: str = "https://cp.example"):
    """The two containers' env, from the real manifest builder."""
    import os

    from kernel.drivers.k8s_spec import pod_manifest
    from kernel.sizes import resolve
    from kernel.spec import ExecutionSpec

    os.environ.setdefault("AGENT_TOKEN_SIGNING_KEY", "test-key-not-real")
    size = resolve("small")
    spec = ExecutionSpec(image="img", command=["sleep", "1"],
                         execution_id=9999, cpus=size.cpus,
                         memory_mb=size.memory_mb,
                         volume_gib=size.volume_gib, node_class=size.name)
    pod = pod_manifest(spec, "exec-9999", namespace="ns", pvc_name="ws-9999",
                       service_account="sa", bridge_image="k:test",
                       control_panel_url=control_panel_url)
    out = {}
    for c in pod["spec"]["containers"]:
        out[c["name"]] = {e["name"] for e in c["env"]}
    return out


def test_the_worker_can_reach_the_endpoint_and_the_sandbox_cannot():
    """The boundary this whole design exists to keep.

    The worker runs our code and needs to store what a leg emitted. The
    sandbox runs the model's, and a token there is one the model can
    use. They are separate containers precisely so this can be true.
    """
    env = _worker_and_sandbox_env()
    from kernel.workspace import podfs_auth

    assert "CONTROL_PANEL_URL" in env["worker"]
    assert podfs_auth.ENV_VAR in env["worker"]
    assert "CONTROL_PANEL_URL" not in env["sandbox"], (
        "the sandbox can now address the control panel's internal "
        "surface, which is model-reachable code")
    assert podfs_auth.ENV_VAR not in env["sandbox"], (
        "the sandbox holds this execution's workspace token")


def test_a_pod_with_no_panel_refuses_rather_than_registering_a_lie():
    """The honesty half, and the one that would have caught this.

    A local put in a pod succeeds and means nothing. Refusing is the
    same discipline `emit_artifact` already applies to credentials:
    "REFUSED, NOT SCRUBBED... a refusal that names the problem".
    """
    src = inspect.getsource(emit._store_durably)
    assert "NotDurable" in src
    assert "in_execution_pod" in src or "AGENTOS_EXECUTION_ID" in src, (
        "nothing distinguishes a pod's local store, which dies with the "
        "run, from E0's, which does not")


def test_the_broker_is_tried_before_the_local_store():
    """Order is the fix. A local put that succeeds first is the bug."""
    src = inspect.getsource(emit._store_durably)
    broker_at = src.index("broker.endpoint")
    local_at = src.index("await aput(data)")
    assert broker_at < local_at, (
        "the local store is consulted before the broker, so a pod would "
        "write to its own disk and never ask the panel")


def test_the_execution_token_authorises_the_write_not_a_global_bearer():
    """Scoping, asserted rather than assumed.

    The pod presents a token proving it is execution N, derived by the
    conductor from AGENT_TOKEN_SIGNING_KEY. A shared internal bearer
    would open every internal route from a pod whose sandbox shares its
    network namespace.
    """
    from app.routers import artifact_object

    src = inspect.getsource(artifact_object)
    # THE IMPORT, NOT THE NAME. The first version of this searched the
    # source for "verify_internal_bearer" and failed on the COMMENT
    # explaining why it is not used -- a check that reads prose and
    # calls it behaviour.
    assert "from app.internal_auth import" not in src, (
        "the artifact endpoint imports the global internal bearer "
        "check; that token would have to live in an execution pod to "
        "be used, and the sandbox shares that pod's network namespace")
    assert "require_execution_token" in src, (
        "the endpoint no longer checks the execution's own token")

    # AND THE HELPER STILL DOES THE WORK. Asserting only that the
    # endpoint CALLS something named like a check is how a check gets
    # hollowed out: the call stays, the comparison leaves with it.
    from app import internal_exec_auth

    helper = inspect.getsource(internal_exec_auth)
    assert "podfs_auth.derive" in helper and "compare_digest" in helper, (
        "the shared helper no longer derives and compares this "
        "execution's token")
    assert "from app.internal_auth import" not in helper, (
        "the shared helper fell back to the global internal bearer")


def test_a_finished_run_may_not_emit():
    """A catalogue somebody trusts should not gain rows after the fact."""
    from app.routers.artifact_object import OPEN_STATES

    for done in ("DONE", "FAILED", "CANCELLED"):
        assert done not in OPEN_STATES


@pytest.mark.parametrize("name", ["loop_tool"])
def test_the_spill_path_stays_local(name):
    """NOT every put should become durable, and this says which.

    `loop_tool` spills oversized TOOL OUTPUT so a leg can range-read its
    own bytes. Its own comment calls storing "an optimisation over
    truncating" -- run-scoped is the honest limit there. Routing it
    through the panel would push every large command's output across a
    web tier for bytes nobody keeps.
    """
    from kernel.harness import loop_tool

    src = inspect.getsource(loop_tool)
    assert "await aput(" in src, f"{name} no longer stores spill locally"
    assert "_store_durably" not in src, (
        f"{name} was routed through the durable path; its output is "
        f"deliberately run-scoped")
