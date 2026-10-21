"""conductor.yaml is applied by a cluster, not merely parsed by us.

THE DEFECT. `ce9251c2` wrote `nodeSelector: {agentos.io/node-class:
platform}` into the conductor, verifier and egress-proxy Deployments so
that control-plane pods would keep off the story pools once Standard
arrived. The manifest was valid YAML, the render was clean, and every
existing test passed. Three consecutive deploys then went red at
`kubectl apply` with

    denied by autogke-node-affinity-selector-limitation

because the cluster being deployed to was still Autopilot, whose Warden
admits a nodeSelector only on keys from its own allowlist. The rejection
is at ADMISSION: nothing was patched, so the cluster went on running the
image from the commit before, and two phases of work sat built and
undeployed while the builds looked merely broken.

WHAT NO TEST COULD SEE. Validity here is not a property of the file. It
is a property of the file AND the cluster, and the only authority is an
API server none of these tests can reach. So this encodes the one rule
that authority applies -- Warden's published allowlist -- and pairs it
with the substitution that lets one file satisfy both clusters while the
migration from Autopilot to Standard is in flight.

The general shape, for the next one: a manifest key that a controller
validates is a contract with the controller, and writing it literally
pins the file to one kind of cluster.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

# THE SAME RENDERER THE DEPLOY USES, reached through the same helper
# every other manifest test uses rather than a second copy of it. A copy
# would keep passing after the real one changed, and its filler
# environment would drift from the manifest's variables -- which is the
# failure this whole file is about, one file away.
from app.services.tests.test_k8s_spec import _rendered

REPO = pathlib.Path(__file__).resolve().parents[3]
MANIFEST = REPO / "deploy" / "k8s" / "conductor.yaml"
DEPLOY = REPO / "scripts" / "infra" / "deploy_execution_plane.sh"

# What GKE Autopilot's Warden admits, quoted from the rejection itself
# rather than from the documentation, and trimmed to the keys anything
# here would plausibly reach for. A key absent from this set is not
# thereby forbidden -- it is unverified, which is the same thing to us.
WARDEN_ALLOWS = frozenset({
    "kubernetes.io/os",
    "kubernetes.io/arch",
    "cloud.google.com/compute-class",
    "cloud.google.com/machine-family",
    "cloud.google.com/gke-spot",
    "cloud.google.com/gke-os-distribution",
    "cloud.google.com/gke-ephemeral-storage-local-ssd",
    "topology.kubernetes.io/region",
    "topology.kubernetes.io/zone",
})

# The two answers the deploy script chooses between.
STANDARD = "agentos.io/node-class: platform"
AUTOPILOT = "kubernetes.io/os: linux"

VARIABLE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _deployments(selector: str) -> dict:
    return {d["metadata"]["name"]: d
            for d in yaml.safe_load_all(
                _rendered(PLATFORM_NODE_SELECTOR=selector))
            if d and d.get("kind") == "Deployment"}


def test_no_deployment_hardcodes_a_key_the_cluster_may_refuse():
    """The literal that cost three deploys, and any key like it.

    Rendered with the Autopilot answer, every nodeSelector key in the
    document must be one Warden admits. A custom key surviving here is
    a key written literally rather than chosen by the deploy script,
    and it fails at apply time on the cluster we are still deploying
    to -- silently, in the sense that the cluster keeps working.
    """
    offenders = {}
    for name, deployment in _deployments(AUTOPILOT).items():
        selector = deployment["spec"]["template"]["spec"].get(
            "nodeSelector") or {}
        bad = sorted(set(selector) - WARDEN_ALLOWS)
        if bad:
            offenders[name] = bad
    assert not offenders, (
        "these Deployments name a nodeSelector key Autopilot's Warden "
        "does not admit, so `kubectl apply` is REJECTED and the cluster "
        "keeps the previous image: " + repr(offenders) + ". Route the "
        "key through the PLATFORM_NODE_SELECTOR substitution, which the "
        "deploy script sets from the cluster's own kind.")


@pytest.mark.parametrize("selector", [STANDARD, AUTOPILOT])
def test_either_answer_renders_a_document_that_parses(selector):
    """Both branches produce YAML, not just the one anyone ran."""
    deployments = _deployments(selector)
    assert set(deployments) == {"conductor", "verifier", "egress-proxy"}
    key, _, value = selector.partition(": ")
    for name, deployment in deployments.items():
        assert deployment["spec"]["template"]["spec"]["nodeSelector"] == {
            key: value}, f"{name} did not take the rendered selector"


def test_the_standard_answer_pins_to_the_platform_pool():
    """Which is the point of the line, and the thing not to lose.

    A future edit that quiets the Warden by deleting the selector
    outright would pass the test above and put the conductor on a story
    node: it takes 1 CPU and 4 GiB from a machine sized for one
    execution, the anti-affinity rule does not exclude it because it is
    not an execution pod, and it evicts the negative-priority warm
    placeholders on its way in. One story per machine, broken quietly.
    """
    for name, deployment in _deployments(STANDARD).items():
        selector = deployment["spec"]["template"]["spec"].get("nodeSelector")
        assert selector == {"agentos.io/node-class": "platform"}, (
            f"{name} does not pin to the platform pool on Standard")


def test_the_deploy_script_exports_every_name_the_manifest_reads():
    """The wire between the two files, which is where this family lives.

    render.py refuses to substitute an unset name, so a variable the
    script forgets to export stops the deploy rather than blanking a
    field. That is the right failure and it is still a failure found in
    Cloud Build; this finds it here.
    """
    exported = set()
    for line in DEPLOY.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            exported.update(stripped.removeprefix("export ").split())
    wanted = set(VARIABLE.findall(MANIFEST.read_text(encoding="utf-8")))
    missing = sorted(wanted - {e.split("=")[0] for e in exported})
    assert not missing, (
        "conductor.yaml reads these and deploy_execution_plane.sh "
        "exports none of them, so render.py stops the deploy: "
        + ", ".join(missing))


def test_the_deploy_script_refuses_a_cluster_it_cannot_identify():
    """Because both wrong guesses are worse than stopping.

    Guessing Standard reproduces the rejected apply on Autopilot.
    Guessing Autopilot puts control-plane pods on story nodes, which is
    the same defect arriving without a red build to announce it.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    assert "autopilot.enabled" in text, (
        "nothing asks the cluster which kind it is")
    _, _, tail = text.partition("autopilot.enabled")
    assert "exit 1" in tail.split("export PLATFORM_NODE_SELECTOR")[0], (
        "the detection has no failing branch, so a cluster we cannot "
        "describe silently takes whichever answer is written last")


def test_a_replicated_platform_pod_is_spread_over_the_pools_machines():
    """Two replicas on one machine are one replica with a larger bill.

    The platform pool holds two e2-standard-4 because the control plane
    does not fit on one -- 3.75 vCPU and 12.5 GiB against about 3.52 and
    11.8 usable. The second machine is bought for survival of the first,
    and it buys nothing unless the replicas actually land apart: with no
    rule, both conductor pods can share a node and a single node loss
    takes the fleet's dispatcher with it.

    PREFERRED rather than required, unlike a story's rule in
    kernel/drivers/k8s_placement.py, and the difference is the failure
    being guarded. A story that shares a machine has already lost its
    isolation, so waiting is better than proceeding; a control plane
    that cannot place a surge replica during a node replacement would
    stall its own rollout, which is worse than a brief co-location.
    """
    deployments = _deployments(STANDARD)
    replicated = {n: d for n, d in deployments.items()
                  if d["spec"].get("replicas", 1) > 1}
    assert replicated, "no multi-replica platform Deployment found"
    for name, deployment in replicated.items():
        spec = deployment["spec"]["template"]["spec"]
        terms = (spec.get("affinity", {}).get("podAntiAffinity", {})
                 .get("preferredDuringSchedulingIgnoredDuringExecution"))
        assert terms, (
            f"{name} has {deployment['spec']['replicas']} replicas and no "
            f"rule keeping them on different machines, so the second "
            f"node it is paying for may hold none of them")
        assert terms[0]["podAffinityTerm"]["labelSelector"][
            "matchLabels"] == {"app": name}
        assert terms[0]["podAffinityTerm"][
            "topologyKey"] == "kubernetes.io/hostname"


def _budgets(selector: str) -> dict:
    return {d["spec"]["selector"]["matchLabels"]["app"]: d
            for d in yaml.safe_load_all(
                _rendered(PLATFORM_NODE_SELECTOR=selector))
            if d and d.get("kind") == "PodDisruptionBudget"}


def test_an_upgrade_cannot_drain_every_replica_at_once():
    """The half the anti-affinity does not cover.

    The platform pool is created with --enable-autoupgrade, so GKE
    drains and replaces these machines unattended. A drain evicts pods
    without asking how many of a Deployment are still serving: node A
    goes, conductor-1 terminates, node B goes before it has rescheduled,
    and for some minutes nothing dispatches a leg. It recovers by
    itself, which is why nobody would find the cause.

    Only a PodDisruptionBudget makes the eviction API wait.
    """
    deployments = _deployments(STANDARD)
    budgets = _budgets(STANDARD)
    replicated = {n for n, d in deployments.items()
                  if d["spec"].get("replicas", 1) > 1}
    missing = sorted(replicated - set(budgets))
    assert not missing, (
        f"{', '.join(missing)} runs more than one replica and has no "
        f"PodDisruptionBudget, so a node upgrade may evict all of them "
        f"at once. Anti-affinity decides where pods land; it does not "
        f"constrain eviction.")


def test_no_budget_can_make_a_drain_block_forever():
    """The footgun on the other side, which is worse than no budget.

    `minAvailable: N` over a Deployment of N replicas can never be
    satisfied while one is being moved, so every drain of that node
    blocks indefinitely. The node upgrade stalls and the symptom is a
    cluster that has been "upgrading" for a week -- which is why the
    single-replica verifier deliberately has no budget at all.
    """
    deployments = _deployments(STANDARD)
    for app, budget in _budgets(STANDARD).items():
        replicas = deployments[app]["spec"].get("replicas", 1)
        floor = budget["spec"].get("minAvailable")
        assert floor is None or floor < replicas, (
            f"{app} has {replicas} replicas and minAvailable={floor}: no "
            f"eviction can ever satisfy this, so node drains on its "
            f"machine will never complete")


def test_the_proxy_restarts_when_its_rules_change():
    """Squid reads its configuration once, at start.

    The allowlist is a ConfigMap mounted into the proxy's pod. A deploy
    that edits it updates the file on disk and leaves the running
    process on the rules it booted with, and Kubernetes has no reason to
    restart the pod because nothing in its spec changed. The deploy goes
    green and the rule that was added is not in force.

    This is not hypothetical. The deploy that added `.nodejs.org`
    behaved exactly so: the ConfigMap in the cluster carried the host,
    and `grep nodejs.org /etc/squid/squid.conf` inside the running pod
    returned nothing. The conductor and verifier rolled because BUILD_SHA
    sits in their environment; the proxy has no such field and had been
    running the same process for two hours.

    The fingerprint annotation is what makes the pod spec change when
    the rules do -- and only when they do, so an image-tag-only deploy
    leaves the proxy up.
    """
    deployments = _deployments(STANDARD)
    proxy = deployments["egress-proxy"]
    annotations = (proxy["spec"]["template"]["metadata"].get("annotations")
                   or {})
    fingerprint = annotations.get("agentos.io/egress-config")
    assert fingerprint, (
        "the egress-proxy pod template carries no fingerprint of its "
        "allowlist, so changing the allowlist will not restart the "
        "process that enforces it")
    assert fingerprint.strip(), "the fingerprint is blank"

    # AND IT MUST BE DERIVED, not typed. A literal would never change,
    # which is the same defect wearing an annotation.
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "${EGRESS_CONFIG_SHA}" in manifest, (
        "the fingerprint is hardcoded in the manifest; it has to be "
        "computed from the allowlist by deploy_execution_plane.sh, or "
        "it will never change when the rules do")
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "egress-allowlist" in deploy and "sha256sum" in deploy, (
        "deploy_execution_plane.sh no longer hashes the allowlist block")
