"""Phase E1's scaffolding: the manifest half of the verifier split.

WHAT THIS FILE IS FOR. `kernel/verifier/service.py` is the entrypoint and
`deploy/k8s/conductor.yaml` is what actually starts it, and the two are
in different languages and only correct together. That is the family of
defect this repository keeps paying for -- `CONTROL_PANEL_URL` was read
in Python, set in YAML by nobody, and cost executions 6, 7 and 8 -- so
the pairs are asserted here rather than reviewed by eye.

THE EXPENSIVE HALF IS THE NETWORK POLICY. The verifier fetches the
committed tree out of the execution pod through podfs on 9101, and
`execution-bridge-ingress` named only `app: conductor`. Both sides of a
connection must agree or the packet is DROPPED, and a default-deny drop
does not reset: the dial HANGS. That is the measured shape of the wedge
(a worker thread in `socket.create_connection` while the event loop sat
idle) and of the squid ingress hole before it, where git waited 132886ms
for a proxy that was up the whole time. A missing line here would read
as a slow criterion for as long as anybody let it.

NOT ASSERTED HERE: that the conductor has STOPPED adjudicating. It has
not, deliberately -- `verdict_lock` makes both safe, and the cutover is
a separate change. A test demanding the conductor's passes be gone would
have to be written and then deleted.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

REPO = pathlib.Path(__file__).resolve().parents[3]
ENTRYPOINT = ["python", "-m", "kernel.verifier.service"]


def _docs():
    from app.services.tests.test_k8s_spec import _rendered

    return [d for d in yaml.safe_load_all(_rendered()) if d]


def _deployment(name: str):
    for doc in _docs():
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == name:
            return doc
    return None


def _policy(name: str):
    for doc in _docs():
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == name:
            return doc
    return None


def _container(deployment, name: str):
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"no container named {name!r}")


# ── the manifest still renders at all ─────────────────────────────────

def test_the_manifest_renders_with_the_verifier_in_it():
    """render.py REFUSES an unset ${...}, so a new variable introduced by
    the verifier block would fail here rather than apply a Deployment
    that names nothing. Nothing new was needed -- it reuses the
    conductor's five -- and this is what says so."""
    names = {(d["kind"], d["metadata"]["name"]) for d in _docs()}
    assert ("Deployment", "conductor") in names
    assert ("Deployment", "verifier") in names


# ── deliverable 1 and 2, asserted against each other ──────────────────

def test_the_verifier_runs_the_module_that_exists():
    """The entrypoint is a string in YAML and a file on disk, and
    nothing but this connects them. A typo is a CrashLoopBackOff whose
    message is one line of a kubectl describe nobody reads on a green
    deploy."""
    container = _container(_deployment("verifier"), "verifier")
    assert container["command"] == ENTRYPOINT
    module = REPO / "kernel" / "verifier" / "service.py"
    assert module.exists(), f"{ENTRYPOINT[-1]} names no module"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    functions = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"main", "run_forever", "verify_pass"} <= functions


def test_it_is_the_same_image_as_the_conductor():
    """Same image, different entrypoint. A second image would need a
    second build, and the two would drift on the day one of them failed
    to build."""
    assert _container(_deployment("verifier"), "verifier")["image"] == \
        _container(_deployment("conductor"), "conductor")["image"]


def test_it_holds_the_identity_that_can_reach_the_api_and_the_store():
    """A KSA OF ITS OWN, and this test used to assert the opposite.

    It argued for sharing `agentos-conductor`: that account already had
    the Role bound and the Workload Identity for the bucket, and "a
    fresh KSA would need both copied and kept identical". The objection
    was sound and it is ANSWERED rather than overruled -- the new KSA
    points at the SAME Google service account, so the Workload Identity
    half is deliberately identical and no GCP capability moved. Only the
    Kubernetes half split.

    WHAT SHARING COST, which is why it could not stand. E1 found that a
    compromised verifier could DELETE EXECUTION PODS -- destroy the very
    work it exists to judge -- because RBAC saw one account. 5.18 made
    it worse by giving the verifier podfs write, so it can also push to
    the forge. Two processes with different blast radii cannot share the
    identity that bounds them.

    The residual is `pods: delete`, which the verifier keeps because
    Kubernetes RBAC has no label selector and `resourceNames` has no
    wildcard: "may delete verify-<id> but not exec-<id>" is not
    expressible. `handover.detach` needs it. What the split DID remove
    is the destructive one -- the verifier can no longer delete
    `ws-exec-<id>`, the claim an execution's workspace lives on. A
    deleted pod is survivable by design (`destroy()` keeps the claim);
    a deleted claim is the work, gone.
    """
    pod = _deployment("verifier")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "agentos-verifier-control"
    # BY NAME, not `next(...)`. The bare version took the FIRST
    # RoleBinding in the document, which is still the conductor's -- so
    # it would have passed on a verifier bound to nothing at all.
    binding = next(d for d in _docs() if d["kind"] == "RoleBinding"
                   and d["metadata"]["name"] == "verifier")
    assert any(s["name"] == "agentos-verifier-control"
               for s in binding["subjects"]), (
        "the verifier's service account is bound to no Role, so every "
        "Job it creates is a 403 the caller reports as a failed check")


def test_it_gets_a_cloud_sql_proxy_exactly_as_the_conductor_does():
    """It pulls its own work out of `executions`, so no database is no
    verifier. A NATIVE sidecar (initContainer + restartPolicy: Always)
    and not an ordinary container: started before this process and
    terminated after it, which is what stops a rolling update cutting a
    transaction off from its own proxy."""
    for name in ("conductor", "verifier"):
        init = _deployment(name)["spec"]["template"]["spec"]["initContainers"]
        proxy = next(c for c in init if c["name"] == "cloud-sql-proxy")
        assert proxy["restartPolicy"] == "Always"
    a = _deployment("conductor")["spec"]["template"]["spec"]["initContainers"]
    b = _deployment("verifier")["spec"]["template"]["spec"]["initContainers"]
    assert a == b, "the two proxies differ; one of them is wrong"


def test_the_grace_period_outlasts_the_drain():
    """`kernel/verifier/service.py` waits AGENTOS_DRAIN_MAX_S (180) for
    the adjudications in flight. A shorter grace period is SIGKILL
    arriving mid-drain, which discards a criterion container already
    paid for -- the fault `LegSupervisor.drain` was written for and had
    no caller."""
    from kernel.worker.service import DRAIN_DEFAULT_S

    pod = _deployment("verifier")["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] > DRAIN_DEFAULT_S


def test_the_liveness_probe_is_the_file_probe_and_not_an_http_one():
    """The kubelet runs an `exec` probe from OUTSIDE the process, which
    is the one property that still works while every coroutine is
    blocked -- and it is how a thirty-minute wedge became a three-minute
    restart. This process runs no HTTP server to probe instead."""
    probe = _container(_deployment("verifier"), "verifier")["livenessProbe"]
    assert probe["exec"]["command"][:3] == ["python", "-m",
                                            "kernel.obs.alive"]
    # THE CONDUCTOR'S DEADLINE, and no longer a second longer. It WAS
    # longer, deliberately, while `dryrun_pass` was serial: one pass was
    # two sequential Jobs per pending question, and a probe firing there
    # re-ran the Jobs it interrupted rather than catching a wedge.
    #
    # E3 ended that -- the passes spawn onto bounded task pools and await
    # no Job -- and E9 ended the dry run itself, so there is one pass
    # left and it still spawns. The concession goes back. A deadline left
    # generous after the reason for it is gone is how a wedge goes
    # unnoticed for thirty minutes, which is the incident
    # `kernel/obs/alive.py` exists for.
    conductor = _container(_deployment("conductor"),
                           "conductor")["livenessProbe"]
    assert float(probe["exec"]["command"][3]) == \
        float(conductor["exec"]["command"][3])


# ── the env the verifier's own code reads ─────────────────────────────

def _env(deployment, container):
    return {e["name"]: e.get("value")
            for e in _container(deployment, container).get("env") or []}


@pytest.mark.parametrize("name", [
    "AGENTOS_DRIVER", "AGENTOS_K8S_NAMESPACE",
    "AGENTOS_K8S_VERIFIER_SERVICE_ACCOUNT",
    "AGENTOS_K8S_CAPTURE_SERVICE_ACCOUNT",
    "AGENTOS_EXECUTION_IMAGE", "AGENTOS_SANDBOX_BASE_IMAGE",
    "AGENTOS_STORE_PROXY", "AGENTOS_EGRESS_PROXY",
    "ARTIFACT_STORE", "ARTIFACT_BUCKET",
])
def test_the_verifier_is_given_what_its_own_passes_read(name):
    """Each of these is read by kernel/verifier/* or by the drivers it
    calls, and an unset one is not an error -- it is a default nobody
    reviewed. AGENTOS_SANDBOX_BASE_IMAGE unset resolved to a literal
    built on one laptop and pushed to no registry: ImagePullBackOff,
    dispatch timeout, reconcile, for ever."""
    assert _env(_deployment("verifier"), "verifier").get(name), (
        f"{name} is unset on the verifier")


def test_the_two_deployments_agree_on_every_value_they_both_carry():
    """A setting that means one thing in the conductor and another in
    the verifier is the 66-branch problem in a new place. They build the
    same drivers from the same class; the values must be the same."""
    conductor = _env(_deployment("conductor"), "conductor")
    verifier = _env(_deployment("verifier"), "verifier")
    shared = set(conductor) & set(verifier)
    differing = {k: (conductor[k], verifier[k]) for k in shared
                 if conductor[k] != verifier[k]}
    assert not differing, differing


def test_the_verifier_does_not_reap():
    """EXACTLY ONE COLLECTOR PER EXTERNAL THING, and it is the
    conductor. `orphan_sweep` destroyed live capture Jobs 7s into a pip
    install because it guessed ownership from a label; a second process
    running the same sweep is that fault with a second actor, and plan
    §9.2 names it as the thing E makes worse rather than better."""
    assert "AGENTOS_RECONCILE_DELETE" not in _env(_deployment("verifier"),
                                                  "verifier")


def test_it_reads_the_bundle_that_carries_the_podfs_signing_key():
    """podfs tokens are DERIVED from AGENT_TOKEN_SIGNING_KEY rather than
    stored, so a verifier without the bundle can open 9101 and be
    refused by every call -- which reads as a broken pod rather than a
    missing secret."""
    container = _container(_deployment("verifier"), "verifier")
    assert [s["secretRef"]["name"] for s in container["envFrom"]] == \
        ["conductor-secrets"]


# ── deliverable 3: both ends of every connection ──────────────────────

def test_the_verifier_may_reach_podfs_on_the_execution_pod():
    """THE RULE THAT DID NOT EXIST. `execution-bridge-ingress` named
    `app: conductor` alone, and adjudication is podfs's biggest caller:
    `archive` and `digest` fetch the tree, `heads` is the lane guard,
    `publish` is how anything reaches the forge."""
    ingress = _policy("execution-bridge-ingress")
    assert ingress, "nothing fronts a shim that runs bash"
    admitted = set()
    for rule in ingress["spec"]["ingress"]:
        for src in rule.get("from") or []:
            app = (src.get("podSelector") or {}).get("matchLabels",
                                                     {}).get("app")
            for port in rule.get("ports") or []:
                admitted.add((app, port["port"]))
    assert ("verifier", 9101) in admitted, (
        "the verifier may not open podfs, and a default-deny drop does "
        "not reset -- the fetch hangs rather than failing")


def test_the_verifier_is_refused_the_sandbox_bridge():
    """9100 runs arbitrary bash in the container the model drives.
    Verification reads trees and writes verdicts; its feedback to the
    executor is a mailbox ROW, not a call into the sandbox. Widening the
    9101 rule to both ports would be one keystroke and is the whole
    reason the two listeners are two ports."""
    ingress = _policy("execution-bridge-ingress")
    for rule in ingress["spec"]["ingress"]:
        peers = {(s.get("podSelector") or {}).get("matchLabels", {}).get("app")
                 for s in rule.get("from") or []}
        if "verifier" not in peers:
            continue
        ports = {p["port"] for p in rule.get("ports") or []}
        assert ports == {9101}, f"the verifier is admitted on {ports}"


def test_the_verifier_has_an_egress_policy_of_its_own():
    """The namespace is default-deny in BOTH directions, so a Deployment
    with no egress rule reaches nothing -- not the Kubernetes API, not
    its own cloud-sql-proxy's instance, not the store. And it fails as a
    hang."""
    policy = _policy("verifier-egress")
    assert policy, (
        "the verifier has no egress policy; default-deny means it "
        "cannot reach the API, Cloud SQL or the artifact store")
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "verifier"}
    assert policy["spec"]["egress"] == [{}]


def test_the_egress_policy_selects_the_label_the_deployment_carries():
    """A selector matching nothing is a policy protecting nothing, and it
    fails open on egress and closed on ingress -- so it has to be read
    off the Deployment rather than off the comment above it."""
    labels = _deployment("verifier")["spec"]["template"]["metadata"]["labels"]
    assert labels["app"] == "verifier"
    assert _policy("verifier-egress")["spec"]["podSelector"][
        "matchLabels"] == labels
    assert _deployment("verifier")["spec"]["selector"]["matchLabels"] == labels


def test_the_two_similarly_named_policies_are_different_things():
    """`verifier-egress` selects the verifier DEPLOYMENT;
    `verifier-egress-to-store` selects `agentos.io/egress: store`, which
    is a CRITERION'S Job and must reach the store and nothing else.
    Collapsing them would hand every check the executor's reach, and a
    check that can reach the internet can fetch a version of itself that
    passes."""
    from kernel.drivers.k8s_egress import LABEL, VIA_STORE

    assert _policy("verifier-egress")["spec"]["podSelector"][
        "matchLabels"] == {"app": "verifier"}
    assert _policy("verifier-egress-to-store")["spec"]["podSelector"][
        "matchLabels"] == {LABEL: VIA_STORE}


def test_nothing_in_this_file_is_labelled_as_a_kernel_container():
    """`managed-by: kernel` means "created for an execution", and the
    orphan sweep destroys what carries it with no matching row. The
    conductor and the proxy wore it once and the reconciler proposed
    deleting them every pass; a Deployment is not a container the kernel
    created."""
    for name in ("conductor", "verifier", "egress-proxy"):
        labels = _deployment(name)["spec"]["template"]["metadata"]["labels"]
        assert "managed-by" not in labels, name


def test_the_deploy_waits_for_the_rollout():
    """`kubectl apply` returning 0 means the API server accepted the
    object. This Deployment is the least likely failure to be noticed,
    because the conductor still adjudicates until the cutover -- so the
    platform keeps working while the new pod crash-loops."""
    script = (REPO / "scripts" / "infra"
              / "deploy_execution_plane.sh").read_text(encoding="utf-8")
    assert "rollout status deployment/verifier" in script
