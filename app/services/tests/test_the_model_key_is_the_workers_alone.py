"""I10, and this is the half that makes it provable.

The invariant reads: "the two-uid boundary holds — after D the model key
is in the WORKER and `bash` still cannot read it." D2 could state the
premise and not prove it, because the key was not in the pod at all:
`POD_ENV_ALLOWLIST` is a closed list and carried none. Its report said
so plainly rather than claiming I10.

THE ROUTE MATTERS AS MUCH AS THE DESTINATION. `POD_ENV_ALLOWLIST` is the
obvious way to get a variable from the conductor into the worker, and it
is the wrong one here: it copies values into `spec.env`, which renders
as literal `{"name": ..., "value": ...}` pairs in the manifest. A key
carried that way is readable with `kubectl get pod -o yaml` by anything
holding pod read on the namespace — which, even after the RBAC split, is
three ServiceAccounts. So it travels as a `secretKeyRef`, the shape
`credential_env` already established for the clone container's token.

AND THE SANDBOX'S ENV IS A CLOSED LITERAL LIST, which is what makes this
provable rather than merely arranged. The sandbox does not inherit
`spec.env`; every variable it gets is written out by hand in
`k8s_spec.pod_manifest`. The boundary is therefore a list somebody has
to deliberately edit, not a filter somebody has to remember to maintain.
"""
from __future__ import annotations

import pytest

from kernel.drivers import k8s_spec
from kernel.spec import ExecutionSpec

SECRET = "conductor-secrets"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN_SIGNING_KEY", "test-key")


def _pod(secret=SECRET):
    return k8s_spec.pod_manifest(
        ExecutionSpec(image="contract:1", command=["sleep"], execution_id=7),
        "exec-7", namespace="ns", pvc_name="ws-7",
        service_account="agentos-execution", bridge_image="bridge:1",
        workspace_secret=secret)


def _container(pod, name):
    for c in pod["spec"]["containers"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"no {name} container")


def _env(pod, name):
    return {e["name"]: e for e in _container(pod, name).get("env") or []}


def test_the_worker_has_the_model_key():
    """The leg runs in the worker once the cutover is on, and a leg with
    no key cannot call a model."""
    assert "ANTHROPIC_API_KEY" in _env(_pod(), "worker")


def test_the_sandbox_does_not():
    """THE INVARIANT. The sandbox is where model-authored `bash` runs.
    A key reachable from there is a key the model can print."""
    sandbox = _env(_pod(), "sandbox")

    for key in k8s_spec.MODEL_KEYS:
        assert key not in sandbox, (
            f"{key} reached the sandbox, where model-authored bash runs")


def test_it_is_a_secret_reference_and_never_a_plain_value():
    """A plain value is readable with `kubectl get pod -o yaml`. The
    allowlist route would have produced exactly that, which is why this
    does not use it."""
    entry = _env(_pod(), "worker")["ANTHROPIC_API_KEY"]

    assert "value" not in entry, "the key is a literal in the manifest"
    ref = entry["valueFrom"]["secretKeyRef"]
    assert ref["name"] == SECRET and ref["key"] == "ANTHROPIC_API_KEY"


def test_no_model_key_is_in_the_plain_env_allowlist():
    """The guard against somebody 'simplifying' this back. Adding a key
    to `POD_ENV_ALLOWLIST` would work, in the sense that the leg would
    find it — and would publish it in the pod spec."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    for key in k8s_spec.MODEL_KEYS:
        assert key not in POD_ENV_ALLOWLIST, (
            f"{key} is on POD_ENV_ALLOWLIST, so it is rendered as a "
            f"literal value in the manifest rather than a secret ref")


def test_a_deployment_with_no_secret_gets_no_key():
    """E0 and the corpus: the leg runs in the conductor there and reads
    the key from its own environment. Requiring a secret would make
    those unable to dispatch at all."""
    assert not [e for e in _container(_pod(secret=None), "worker").get("env")
                or [] if e["name"] in k8s_spec.MODEL_KEYS]


def test_every_key_is_optional():
    """A deployment configures the vendor it uses. Requiring both would
    make a pod with only an Anthropic key fail to START — and a pod that
    will not start is a story that cannot report why. The adapter says
    which key it needs and fails with a sentence."""
    worker = _env(_pod(), "worker")

    for key in k8s_spec.MODEL_KEYS:
        assert worker[key]["valueFrom"]["secretKeyRef"]["optional"] is True


def test_the_sandbox_env_is_still_a_closed_list():
    """What makes the boundary provable. If the sandbox ever inherited
    `spec.env`, every test above would keep passing while the property
    they describe quietly stopped holding."""
    import pathlib

    src = pathlib.Path("kernel/drivers/k8s_spec.py").read_text(
        encoding="utf-8")
    sandbox_block = src.split('"name": SANDBOX')[1]
    assert "spec.env.items()" not in sandbox_block.split('"resources"')[0], (
        "the sandbox container now inherits spec.env, so its environment "
        "is no longer a list somebody has to deliberately edit")


# ── and WHICH model, which is a different fact from which key ──────────

def test_the_pod_is_told_which_model_to_use():
    """Found by asking "will Anthropic work for executors?" — and it
    would not have.

    `AGENTOS_ASSIST_MODEL` was not on the allowlist and not on the
    manifest, and both `pod_main` and `service_tick` fall back to
    "gemini". So a deployment set to `anthropic` ran Anthropic in the
    conductor and GEMINI IN EVERY POD — a different model, no error, and
    nothing in the log naming which one answered. The bill would have
    said so eventually.

    Same class as the base-image bug the allowlist's own comment
    records: a fallback correct for one process and silently wrong for
    the other, in a process whose environment IS this list.
    """
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    assert "AGENTOS_ASSIST_MODEL" in POD_ENV_ALLOWLIST


def test_the_choice_travels_as_a_value_and_the_key_does_not():
    """Two different facts with two different shapes. WHICH model is not
    a secret and rides the allowlist as a plain value; the KEY is a
    secretKeyRef on the worker alone. Putting the key on the allowlist
    would publish it in the pod spec, which is what the rest of this
    file is about."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    for key in k8s_spec.MODEL_KEYS:
        assert key not in POD_ENV_ALLOWLIST


def test_both_processes_share_one_fallback():
    """ONE SOURCE BY CONSTRUCTION, which is better than what this test
    used to check.

    It asserted the literal `"gemini"` appeared in both files -- two
    copies that had to be kept equal by hand, and which were NOT equal:
    `service_tick` said "gemini" (a provider) and `quick` said
    "gemini-3.8-flash" (a model id), for the same variable. That is the
    conflation the model-choice work had to fix first.

    Both now call `choice.default_model()`, so there is nothing to keep
    in step. The literal lives once, in `choice.FALLBACK`.
    """
    import pathlib

    from kernel.harness.models import choice

    for rel in ("kernel/worker/pod_main.py", "kernel/worker/service_tick.py"):
        src = pathlib.Path(rel).read_text(encoding="utf-8")
        assert "choice.default_model()" in src, (
            f"{rel} has its own fallback again; two copies of this "
            f"literal were previously two DIFFERENT literals")

    from kernel.harness.models.registry import SUPPORTED

    assert choice.FALLBACK in SUPPORTED, (
        "the one fallback is not a model the adapters can serve")


def test_the_pod_is_told_which_substrate_it_is_on():
    """MEASURED ON EXECUTION 213: `KeyError: 'DOCKER_HOST'`.

    `registry.ENV` is `AGENTOS_DRIVER` and its DEFAULT is "docker". A
    worker not told otherwise builds a Docker driver inside a Kubernetes
    pod and reaches for a socket that does not exist. The conductor has
    the variable; the pod's entire environment IS this allowlist.

    THIRD INSTANCE OF ONE PATTERN -- AGENTOS_EXECUTION_ID, then
    AGENTOS_ASSIST_MODEL, then this. And this one is why a grep is not
    enough: `registry.driver_name` reads the variable through a
    CONSTANT, so an audit searching for `os.environ["AGENTOS_..."]`
    finds the other two and not this.
    """
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST
    from kernel.drivers.registry import ENV as DRIVER_ENV

    assert DRIVER_ENV in POD_ENV_ALLOWLIST, (
        f"{DRIVER_ENV} is not passed to the pod, so the leg builds a "
        f"{__import__('kernel.drivers.registry', fromlist=['DEFAULT']).DEFAULT} "
        f"driver wherever it runs")


def test_what_the_leg_needs_at_runtime_travels_with_it():
    """The pod's environment is this list and nothing else. Each of
    these is read on the leg's own path: the profile it resolves, the
    runtime dir its git credentials live under, the workspace root, and
    the model pair."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    for name in ("AGENTOS_WORKSPACE_ROOT", "AGENTOS_PROFILES_DIR",
                 "AGENTOS_RUNTIME_DIR", "AGENTOS_ASSIST_MODEL"):
        assert name in POD_ENV_ALLOWLIST, f"{name} never reaches the leg"
