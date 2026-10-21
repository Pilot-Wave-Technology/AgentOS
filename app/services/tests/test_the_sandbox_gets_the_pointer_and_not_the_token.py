"""Two variables, one pod, and each has to land on exactly one container.

WHAT WAS TRUE BEFORE, AND BOTH HALVES WERE MEASURED.

THE POINTER THE POD PATH NEVER SET (B3). `AGENTOS_CREDENTIALS` names the
directory a credential lease is written to. `conductor/sandbox_spec.py`
has set it on the DOCKER sandbox since Ec4d, and
`bridge/tools_core._safe_env` -- the allowlist applied inside the sandbox
before `bash` sees anything -- has carried the name since the same
change. The Kubernetes sandbox container's `env` in `drivers/k8s_spec.py`
is a separate, deliberately CLOSED list of literals, and it did not have
it. So a goal holding a cloud grant dispatched to GKE, the lease landed
in `/workspace/.agentos/credentials`, the allowlist had a name and no
value to forward, and the model reported that it had no credentials. The
symptom is a permission error out of a cloud SDK, which is three layers
and one container away from the list that dropped it.

THE TOKEN THAT MUST NOT REACH THE MODEL (B4a). `podfs` binds
`0.0.0.0:9101` in the WORKER container. The sandbox is a container in
the same pod, so it shares the network namespace, so the model's own
`bash` reached it on loopback -- verified live on exec-193, nine
operations, and they are not readers: `commit` pushes with the story's
git identity, `credentials` places a lease, `revoke_credentials` removes
one. `kernel/workspace/podfs_auth.py` closes that by deriving a
per-execution token the worker checks and the sandbox is never given.

WHY THE TEST IS SHAPED LIKE THIS.

Because the same defect has now been fixed twice and shipped broken
twice, in both directions, and each time the check was done somewhere the
subject does not travel. `HTTP_PROXY` and `GIT_CONFIG_*` were added to
the sandbox container and dropped by `_safe_env`, and the verification
was `kubectl exec`, which inherits the container's env directly and never
passes through the allowlist -- so it worked by hand and would have
failed for the model. A container variable that `_safe_env` drops is a
fix that does nothing; a secret kept out of one list and not the other is
not kept out.

So the two lists are compared DIRECTLY and behaviourally: the pod is
built for real and its containers' `env` are read out of the returned
dict, and `_safe_env` is exercised with those names actually in the
environment. Nothing here greps source text, and no variable name is
spelled a second time -- `podfs_auth.ENV_VAR` and
`credentials.ENV_POINTER` are imported from the modules that declare
them, because a guard that agrees with a typo is not a guard.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

# `tools_core` is written to run INSIDE the sandbox, where the shim puts
# `kernel/bridge` on sys.path and its siblings import bare (`import
# errors`). Reproduce that here rather than rewrite the module for the
# test -- the module under test should be the one that ships.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]
                       / "kernel" / "bridge"))

from kernel.bridge.tools_core import _safe_env              # noqa: E402
from kernel.drivers.k8s_spec import (SANDBOX, WORKER,       # noqa: E402
                                     pod_manifest)
from kernel.spec import ExecutionSpec                       # noqa: E402
from kernel.workspace import podfs_auth                     # noqa: E402
from kernel.workspace.credentials import ENV_POINTER        # noqa: E402

EXECUTION_ID = 7


@pytest.fixture
def signed(monkeypatch):
    """The conductor's signing key, which `pod_manifest` now requires.

    Deliberately: without it no workspace token can be derived, the
    worker cannot be handed the one it checks, and the pod would come up
    2/2 and refuse every workspace call -- which reads as a hung
    execution rather than as an unset secret.
    """
    monkeypatch.setenv(podfs_auth.SIGNING_KEY_VAR, "signing-key-for-a-test")


def _pod(execution_id: int | None = EXECUTION_ID) -> dict:
    """A real manifest, and the assertions read the dict it returns."""
    spec = ExecutionSpec(image="registry/sandbox:1",
                         execution_id=execution_id,
                         story_id="s1",
                         env={"ARTIFACT_STORE": "gs://bucket"})
    return pod_manifest(spec, f"exec-{execution_id}", namespace="ns",
                        pvc_name=f"ws-exec-{execution_id}",
                        service_account="sa",
                        bridge_image="registry/kernel:abc",
                        egress_proxy="http://egress-proxy:3128")


def _env(pod: dict, container: str) -> dict:
    for entry in pod["spec"]["containers"]:
        if entry["name"] == container:
            return {e["name"]: e["value"] for e in entry["env"]}
    raise AssertionError(f"the pod has no {container} container")


# ── B3: the pointer reaches the sandbox, and survives the second list ──

def test_the_sandbox_is_told_where_its_credential_lease_is(signed):
    """A path, not a secret. Absent, a granted run finds nothing."""
    value = _env(_pod(), SANDBOX).get(ENV_POINTER)
    assert value, (
        f"{ENV_POINTER} is not on the pod's sandbox container, so a run "
        f"holding a cloud grant has a lease on disk and nothing naming "
        f"the directory it is in.")
    assert value.startswith("/workspace/"), (
        f"{ENV_POINTER} must name a directory inside the container's own "
        f"workspace mount, not the conductor's idea of a path: {value!r}")


def test_the_pointer_the_container_holds_is_the_pointer_bash_gets(
        signed, monkeypatch):
    """THE SECOND ALLOWLIST, travelled rather than read.

    `kubectl exec` inherits the container env directly, so checking it
    that way proves nothing about what a tool call receives.
    """
    monkeypatch.setenv(ENV_POINTER, _env(_pod(), SANDBOX)[ENV_POINTER])
    assert _safe_env().get(ENV_POINTER) == _env(_pod(), SANDBOX)[ENV_POINTER]


def test_nothing_set_on_the_sandbox_is_dropped_before_bash(
        signed, monkeypatch):
    """The two lists, compared directly and in the direction that bites.

    Every name the manifest puts on the sandbox is placed in a real
    environment and `_safe_env` is asked what survives. A variable added
    to the container from now on either reaches the shell or fails here
    with its own name in the message.

    CASE-FOLDED, and not out of laziness. Windows' `os.environ` is
    case-insensitive, so setting `http_proxy` stores `HTTP_PROXY` and the
    lowercase half of every proxy pair looks dropped on a developer's
    laptop and not in CI. A guard that fails on one platform for a reason
    that is not the defect gets disabled. The allowlist carries both
    spellings of every pair it carries at all, so folding costs nothing
    real here.
    """
    given = _env(_pod(), SANDBOX)
    for name, value in given.items():
        monkeypatch.setenv(name, value)
    survived = {name.upper() for name in _safe_env()}
    dropped = sorted(name for name in given if name.upper() not in survived)
    assert not dropped, (
        f"these are set on the sandbox container and dropped by "
        f"_safe_env, so a tool call never sees them: {dropped}")


# ── B4a: the token reaches the worker and nothing else ────────────────

def test_the_worker_can_prove_it_is_the_conductor(signed):
    """And the conductor can reproduce what the worker will check."""
    value = _env(_pod(), WORKER).get(podfs_auth.ENV_VAR)
    assert value == podfs_auth.derive(EXECUTION_ID), (
        f"the worker must carry {podfs_auth.ENV_VAR} for this execution; "
        f"without it `podfs` refuses every call and the execution hangs "
        f"rather than fails.")


def test_the_sandbox_is_never_given_the_workspace_token(signed):
    """The sandbox shares the worker's network namespace. This value is
    the only thing standing between the model's own `bash` and `commit`,
    `credentials` and `revoke_credentials` on loopback."""
    assert podfs_auth.ENV_VAR not in _env(_pod(), SANDBOX)


def test_the_allowlist_would_not_forward_the_token_either(
        signed, monkeypatch):
    """BOTH LISTS, because one of them is not enough.

    If the token ever did leak onto the sandbox container,
    `_safe_env` must still refuse to hand it to a shell.
    """
    monkeypatch.setenv(podfs_auth.ENV_VAR, _env(_pod(), WORKER)[
        podfs_auth.ENV_VAR])
    assert podfs_auth.ENV_VAR not in _safe_env()


# ── the derivation the whole arrangement rests on ─────────────────────

def test_a_token_is_this_execution_and_no_other(signed):
    """Deterministic, so two processes that never meet agree; and keyed
    by execution, so a token that leaks opens one workspace."""
    assert podfs_auth.derive(EXECUTION_ID) == podfs_auth.derive(EXECUTION_ID)
    assert podfs_auth.derive(EXECUTION_ID) != podfs_auth.derive(
        EXECUTION_ID + 1)


def test_a_pod_that_cannot_be_authenticated_is_not_built(monkeypatch):
    """No key, no pod. The alternative is a pod that starts, looks
    healthy at 2/2, and refuses every workspace operation -- which reads
    as a hung execution rather than as an unset secret."""
    monkeypatch.delenv(podfs_auth.SIGNING_KEY_VAR, raising=False)
    with pytest.raises(podfs_auth.TokenMissing):
        _pod()


def test_a_manifest_with_no_execution_id_carries_no_token_at_all(
        monkeypatch):
    """NOT A BLANK TOKEN AND NOT A SHARED ONE -- no variable.

    `derive` is a pure function of the execution id, so `None` would give
    every id-less pod the same token and one leak would open all of them.
    An id-less manifest is an INSPECTED manifest -- no dispatch path
    builds one, `kubernetes.py` records that finding, and inspecting a
    translation must not require a production secret. If such a pod were
    somehow dispatched, `podfs.serve()` refuses to start on the absent
    variable, so it fails closed rather than open.
    """
    monkeypatch.delenv(podfs_auth.SIGNING_KEY_VAR, raising=False)
    worker = _env(_pod(execution_id=None), WORKER)
    assert podfs_auth.ENV_VAR not in worker
    assert not [v for v in worker.values() if not v], (
        "an empty value would authorise an empty header, which is every "
        "caller")
