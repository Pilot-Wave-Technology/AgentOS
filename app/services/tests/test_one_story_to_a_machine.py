"""A machine runs one story, and two separate mechanisms say so.

WHY THIS IS NOT ONE TEST ABOUT ONE SETTING. "One story per node" sounds
like a single switch and is actually two claims that fail independently:

    WHERE   the nodeSelector puts the pod in the pool whose machines are
            sized for that tier. Wrong pool, wrong machine size.
    HOW MANY  the pod anti-affinity keeps anything else off the machine
            once it is there. Right pool, two stories to a node.

Each looks correct on its own while the pair is broken, which is the
shape this repository keeps paying for. The selector was added first and
alone; a reviewer reading it would reasonably conclude the job was done,
because the words "node class" appear and a pod does end up on the right
kind of machine. It just does not end up there alone: the default
scheduler packs, and two 3.25-CPU stories fit an e2-standard-4 with room
to think about it.

Neither failure produces an error. Two stories on one machine both run,
both finish, and both take rather worse than twice as long as one --
they contend for cache and memory bandwidth, not only for cores. The
only symptom is that the fleet got slower, with no event to point at.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from kernel.drivers.k8s_job import job_manifest
from kernel.drivers.k8s_placement import (EXECUTION, KIND_LABEL, TOPOLOGY,
                                          VERIFICATION, WORK, kind_label,
                                          one_per_node)
from kernel.drivers.k8s_spec import NODE_CLASS_LABEL, pod_manifest
from kernel.spec import NODE_CLASSES, ExecutionSpec

REPO = pathlib.Path(__file__).resolve().parents[3]
WARM_POOL = REPO / "deploy" / "k8s" / "warm-pool.yaml"

REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"


def _spec(**kw) -> ExecutionSpec:
    base = dict(image="agentos:1", execution_id=7, cpus=2.5, memory_mb=9216)
    base.update(kw)
    return ExecutionSpec(**base)


def _execution_pod(**kw) -> dict:
    return pod_manifest(_spec(**kw), "exec-7", namespace="agentos-staging",
                        bridge_image="registry/kernel:test",
                        pvc_name="ws-7", service_account="agentos-exec")


def _verification_pod(**kw) -> dict:
    job = job_manifest(_spec(**kw), "crit-1", namespace="agentos-staging",
                       timeout_s=600, service_account="agentos-verifier")
    return {"metadata": job["spec"]["template"]["metadata"],
            "spec": job["spec"]["template"]["spec"]}


def _prepare_pod(**kw) -> dict:
    """THE THIRD POD KIND, and the one the first pass of this work missed.

    A prepare Job reruns the story's dependency install, so it asks for
    the story's whole tier -- and while the tiers were one vCPU that was
    a rounding error nobody had to place deliberately. At fourteen it is
    a machine's worth, and a Job carrying no kind label neither repels
    nor is repelled: pod anti-affinity constrains the pod being
    scheduled against pods already present, so a story's own rule cannot
    keep a later arrival off its machine. It landed wherever the cores
    happened to be free, which includes a node already running a story.
    """
    from kernel.verifier.prepare_job import prepare_manifest

    job = prepare_manifest(_spec(**kw), "prep-1", namespace="agentos-staging",
                           claim="verify-7", service_account="agentos-verifier",
                           timeout_s=600, kernel_image="registry/kernel:test",
                           source="{}", token="t")
    return {"metadata": job["spec"]["template"]["metadata"],
            "spec": job["spec"]["template"]["spec"]}


POD_KINDS = [
    pytest.param(_execution_pod, EXECUTION, id="execution"),
    pytest.param(_verification_pod, VERIFICATION, id="verification"),
    pytest.param(_prepare_pod, VERIFICATION, id="prepare"),
]


@pytest.mark.parametrize("build,kind", POD_KINDS)
def test_work_says_that_it_is_work(build, kind):
    """The label is what makes a pod visible to the rule.

    A pod that does the work and does not carry this is not repelled and
    does not repel: it shares a machine happily in both directions.
    """
    assert build()["metadata"]["labels"][KIND_LABEL] == kind


@pytest.mark.parametrize("build,_kind", POD_KINDS)
def test_work_refuses_to_share_a_machine_with_other_work(build, _kind):
    """And repels the OTHER kind too, which is the easy half to miss.

    A rule written as "no other pod of my own kind" would let a verifier
    land on a busy story's machine -- a criterion re-running that story's
    test suite, competing with the story for the cores it was sized for,
    and then reporting on how long the tests took.
    """
    rules = build()["spec"]["affinity"]["podAntiAffinity"][REQUIRED]
    assert len(rules) == 1
    expressions = rules[0]["labelSelector"]["matchExpressions"]
    assert expressions == [{"key": KIND_LABEL, "operator": "In",
                            "values": list(WORK)}]
    assert set(WORK) == {EXECUTION, VERIFICATION}


@pytest.mark.parametrize("build,_kind", POD_KINDS)
def test_the_rule_is_required_rather_than_preferred(build, _kind):
    """Preferred holds until the fleet is busy, and then stops.

    `preferredDuringScheduling` is advice the scheduler drops under
    pressure -- which is to say it is honoured whenever it does not
    matter and abandoned at the moment it does. The price of REQUIRED is
    that a story waits for a machine rather than sharing one, and with
    an autoscaling pool that wait is the ninety seconds a node takes.
    """
    affinity = build()["spec"]["affinity"]["podAntiAffinity"]
    assert REQUIRED in affinity
    assert "preferredDuringSchedulingIgnoredDuringExecution" not in affinity


@pytest.mark.parametrize("build,_kind", POD_KINDS)
def test_the_rule_spreads_over_machines_and_not_zones(build, _kind):
    """`kubernetes.io/hostname` is the machine.

    A topologyKey of zone or region reads almost the same and claims
    something far weaker -- one story per ZONE would leave a
    twenty-machine pool running three stories.
    """
    rules = build()["spec"]["affinity"]["podAntiAffinity"][REQUIRED]
    assert rules[0]["topologyKey"] == TOPOLOGY == "kubernetes.io/hostname"


@pytest.mark.parametrize("build,_kind", POD_KINDS)
@pytest.mark.parametrize("node_class", NODE_CLASSES)
def test_work_names_the_pool_it_belongs_in(build, _kind, node_class):
    """The other half. A criterion gets the story's pool for the same
    reason the story did: it re-runs the story's work, and a small
    machine for a large story reports a timeout rather than a shortage.
    """
    pod = build(node_class=node_class)
    assert pod["spec"]["nodeSelector"] == {NODE_CLASS_LABEL: node_class}


def test_the_warm_placeholders_are_invisible_to_the_rule():
    """Because a pause pod exists to be evicted BY the work.

    Its whole job is to hold a machine open so a real story can preempt
    it and start in seconds rather than in the time a node takes to
    boot. Give it `agentos.io/kind` and it repels the story it is
    keeping the seat warm for -- the placeholder then holds a machine no
    story may use, which is the one outcome worse than having no warm
    pool at all.
    """
    if not WARM_POOL.exists():                         # pragma: no cover
        pytest.skip("warm-pool.yaml is not in this checkout")
    text = WARM_POOL.read_text(encoding="utf-8").replace(
        "${NAMESPACE}", "agentos-staging")
    templates = [d["spec"]["template"] for d in yaml.safe_load_all(text)
                 if d and d.get("kind") == "Deployment"]
    assert templates, "no placeholder Deployments found"
    for template in templates:
        assert KIND_LABEL not in (template["metadata"].get("labels") or {}), (
            "a warm placeholder carries the work label and will repel "
            "the story it is holding a machine for")


def test_a_pod_that_is_not_work_cannot_ask_for_the_label():
    """The refusal exists because the placeholders are one typo away.

    `kind_label("warm")` returning `{"agentos.io/kind": "warm"}` would be
    accepted by Kubernetes, ignored by the `In` selector, and wrong in a
    way nothing reports.
    """
    with pytest.raises(ValueError) as caught:
        kind_label("warm")
    assert "warm" in str(caught.value)
    assert KIND_LABEL in str(caught.value)


def test_the_rule_is_one_object_shared_by_both_kinds():
    """Not two hand-written blocks that can drift apart.

    Two copies is how the selector and the label came to disagree in the
    first place: each was edited on its own, by someone reading only the
    file they were in.
    """
    assert one_per_node() == _execution_pod()["spec"]["affinity"]
    assert one_per_node() == _verification_pod()["spec"]["affinity"]
