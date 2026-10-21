"""A pod is pointed at the proxy port its own label opens.

WHAT WAS TRUE BEFORE, AND IT HANGS RATHER THAN FAILING.

The namespace is default-deny and two NetworkPolicies decide which port
of squid a pod may use, selected on one label:

    agentos.io/egress: proxy   ->  TCP 3128, the full allowlist
    agentos.io/egress: store   ->  TCP 3129, the object store alone

The label was spelled in four places; the proxy ADDRESS was chosen in a
fifth. Nothing made them agree, and they did not:

  * `k8s_capture.capture_manifest` labels its pod `proxy`.
  * `k8s_ephemeral.run_capture` handed that pod `fetch_env()`, which set
    HTTP_PROXY from AGENTOS_STORE_PROXY -- 3129 -- for BOTH the
    `fetch-tree` init container and the `capture` sidecar.

3129 out of a `proxy`-labelled pod is denied twice: by that pod's egress
rule, and by `egress-proxy-ingress`, which accepts 3129 only from a
`store`-labelled pod. Default-deny DROPS rather than resetting, so the
connection does not fail -- it hangs. The measured version of this class
on the executor side was `pip install` never returning and holding the
bridge's tool lock for twenty minutes. Here it is a capture that cannot
fetch the tree it replays and a sidecar that cannot upload the directory
it captured: both directions of the artifact store, on the one path a
criterion needs to run at all.

AND THE COMMENT THAT MADE IT LOOK FINE. `capture_manifest` said the pod
"carries `proxy` as well, and the broader of the two is what the pod
gets". That is true of squid's allowlist and false of the NetworkPolicy,
which selects on a single value -- `proxy` REPLACES `store`, and closes
3129 to the pod. A comment is not something the second caller has to
satisfy.

WHY THE TEST IS SHAPED LIKE THIS. It does not assert "the capture pod
uses 3128", which is just the same constant written a second time and
would pass if both halves were wrong together. It reads the label off the
manifest the builder actually produced, derives the permitted address
from that label, and asserts the containers were given THAT. Change
either half alone and this fails.
"""
from __future__ import annotations

import pytest

from kernel.drivers import k8s_egress
from kernel.drivers.k8s_egress import (LABEL, VIA_CAPTURE, VIA_PROXY,
                                       VIA_STORE)
from kernel.spec import ExecutionSpec

FULL = "http://egress-proxy:3128"
STORE = "http://egress-proxy:3129"


@pytest.fixture(autouse=True)
def _a_cluster_with_both_proxies(monkeypatch):
    """The conductor holds both addresses and passes them on."""
    monkeypatch.setenv("AGENTOS_EGRESS_PROXY", FULL)
    monkeypatch.setenv("AGENTOS_STORE_PROXY", STORE)
    monkeypatch.setenv("AGENTOS_EXECUTION_IMAGE", "reg/plane:abc")
    monkeypatch.setenv("ARTIFACT_BUCKET", "a-bucket")
    # The proxy is only set for a real store; `filesystem` is E0.
    monkeypatch.setattr("kernel.artifacts.store.BINDING", "gcs")


def _spec() -> ExecutionSpec:
    return ExecutionSpec(image="reg/fixture:1", command=["true"],
                         execution_id=7)


def _env_of(container: dict) -> dict:
    return {e["name"]: e["value"] for e in (container.get("env") or [])}


def _pod(manifest: dict) -> dict:
    return manifest["spec"]["template"]


def _all_containers(manifest: dict) -> list:
    pod = _pod(manifest)["spec"]
    return list(pod.get("initContainers") or []) + list(pod["containers"])


# ── the derivation itself ────────────────────────────────────────────

def test_each_class_resolves_to_its_own_port():
    assert k8s_egress.proxy_for(VIA_PROXY) == FULL
    assert k8s_egress.proxy_for(VIA_STORE) == STORE


def test_an_unknown_class_is_refused_rather_than_defaulted():
    """A typo falling back to `store` would give a setup script the
    object store's allowlist and a hang on the first package index --
    this module's own bug with a different spelling."""
    with pytest.raises(k8s_egress.UnknownEgress):
        k8s_egress.proxy_for("egress")


def test_no_deployment_proxy_means_no_proxy_variables(monkeypatch):
    """E0 and the fixtures: there is no proxy and no policy to need one,
    and the callers already read "" as "set nothing"."""
    monkeypatch.delenv("AGENTOS_EGRESS_PROXY", raising=False)
    assert k8s_egress.proxy_for(VIA_PROXY) == ""


# ── THE REGRESSION: the label and the port must agree ────────────────

@pytest.mark.parametrize("build", ["capture", "criterion"])
def test_every_container_is_given_the_port_its_pod_label_opens(build):
    """The one that would have caught it. The expected value is DERIVED
    from the manifest's own label, not written here a second time."""
    from kernel.drivers.k8s_capture import capture_manifest
    from kernel.drivers.k8s_ephemeral import fetch_env
    from kernel.drivers.k8s_job import job_manifest

    fetch = [("/tmp/verify", "art-1")]
    if build == "capture":
        env = fetch_env(VIA_CAPTURE)
        manifest = capture_manifest(
            _spec(), "cap-1", namespace="ns", timeout_s=60,
            service_account="sa", capture_dir="/deps",
            capture_image="reg/plane:abc", capture_env=env,
            fetch=fetch, fetch_image="reg/plane:abc", fetch_env=env)
    else:
        manifest = job_manifest(
            _spec(), "crit-1", namespace="ns", timeout_s=60,
            service_account="sa", fetch=fetch,
            fetch_image="reg/plane:abc", fetch_env=fetch_env(VIA_STORE))

    label = _pod(manifest)["metadata"]["labels"][LABEL]
    permitted = k8s_egress.proxy_for(label)

    saw_one = False
    for container in _all_containers(manifest):
        env = _env_of(container)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                     "https_proxy"):
            if name in env:
                saw_one = True
                assert env[name] == permitted, (
                    f"{build}: container {container.get('name')!r} is "
                    f"pointed at {env[name]} but its pod is labelled "
                    f"{label!r}, which opens {permitted}. A denied port "
                    f"does not refuse, it hangs.")
    assert saw_one, (
        f"{build}: no container carries a proxy variable at all, so it "
        f"reaches for the store directly and is dropped by the policy")


def test_the_capture_pod_has_its_own_class_not_the_executors():
    """`capture`, and not `proxy` -- CHANGED AFTER EXECUTION 205.

    It was `proxy`, which is right about the port and wrong about one
    rule. The capture Job's WORK container runs the tree's own setup
    script and needs the full allowlist; its SIDECAR calls
    `storage.Client()`, which takes the project id and the token from
    169.254.169.254. `workload-egress-via-proxy` does not admit that
    address, so pip exited 0 and the upload died:

        capture: OSError: Project was not passed and could not be
                 determined from the environment

    -- an empty archive, every criterion `error`, and the run looping
    VERIFYING -> RUNNING for ever.

    The rule could not simply be added to `proxy`, because that label
    ALSO selects the execution pod, where the sandbox shares the network
    namespace -- see the test below, which is the other half of this
    one."""
    from kernel.drivers.k8s_capture import capture_manifest

    manifest = capture_manifest(
        _spec(), "cap-1", namespace="ns", timeout_s=60,
        service_account="sa", capture_dir="/deps",
        capture_image="reg/plane:abc")
    assert _pod(manifest)["metadata"]["labels"][LABEL] == VIA_CAPTURE


def test_capture_and_proxy_share_a_port_so_the_split_costs_nothing():
    """The two classes differ in POLICY, not in route. If they ever
    resolved to different addresses, the capture Job would be labelled
    for one port and pointed at the other -- which does not fail, it
    hangs, and is the original bug this file was written for."""
    assert k8s_egress.proxy_for(VIA_CAPTURE) == k8s_egress.proxy_for(
        VIA_PROXY)


def test_a_criterion_is_still_held_to_the_narrow_class():
    """The containment property, asserted so a later widening fails
    here: a criterion that can reach the forge can fetch a version of
    itself that passes."""
    from kernel.drivers.k8s_job import job_manifest

    manifest = job_manifest(_spec(), "crit-1", namespace="ns",
                            timeout_s=60, service_account="sa")
    assert _pod(manifest)["metadata"]["labels"][LABEL] == VIA_STORE


# ── and the half-set pair, which is the same hang ────────────────────

def test_both_cases_are_set():
    """curl reads lowercase `http_proxy`, most Python tooling reads
    either. A half-set pair is a tool that silently bypasses the proxy
    and hangs exactly as before."""
    env = fetch = None
    from kernel.drivers.k8s_ephemeral import fetch_env

    env = fetch_env(VIA_PROXY)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        assert env[name] == env[name.lower()], f"{name} is half-set"


def test_in_cluster_names_do_not_go_through_squid():
    from kernel.drivers.k8s_ephemeral import fetch_env

    skip = fetch_env(VIA_STORE)["NO_PROXY"]
    for name in ("localhost", "127.0.0.1", ".svc", ".cluster.local"):
        assert name in skip


def test_a_filesystem_store_gets_no_proxy_at_all(monkeypatch):
    """E0 writes artifacts to a directory; there is nothing to proxy and
    no policy to satisfy."""
    from kernel.drivers.k8s_ephemeral import fetch_env

    monkeypatch.setattr("kernel.artifacts.store.BINDING", "filesystem")
    env = fetch_env(VIA_PROXY)
    assert "HTTP_PROXY" not in env and "http_proxy" not in env


def test_the_label_is_declared_once():
    """Four spellings of the label string is how it came apart. The two
    manifest builders must read it from k8s_egress, not retype it."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    for rel in ("kernel/drivers/k8s_capture.py", "kernel/drivers/k8s_job.py"):
        text = (root / rel).read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("#"))
        assert '"agentos.io/egress"' not in code, (
            f"{rel} spells the label again instead of importing LABEL")
