"""A pod lands on its own environment's machines, where the pools say so.

ONE CLUSTER, MORE THAN ONE ENVIRONMENT. `agentos-cluster` is to run `next`
and `dev`, and each needs a pool of every class -- the class is a SHAPE,
and a dev story needs the same shape a next story does. So
`agentos.io/node-class: small` names a kind of machine, of which there
are two sets, and a pod selecting only the class lands on whichever set
has room.

WHAT GOES WRONG IS NOTHING, VISIBLY. Both pods are admitted, both
schedule, both run. A dev story simply takes a whole machine that `next`
is paying for, under one-story-per-node, and the only trace is an
invoice. That is the argument for a label the scheduler enforces rather
than a convention nobody checks.

── AND WHY IT IS OPT-IN, PERMANENTLY ──────────────────────────────────

`next` is the production instance of this architecture and is never
brought into the scheme. Every way of doing so modifies production:
relabelling a pool rolls it, rolling evicts a running story, and the
re-dispatch costs a leg off the twelve-leg convergence ceiling for work
that may be hours in.

So the isolation is ASYMMETRIC and lives entirely on the new pools:

    dev's pods   select node-class AND env=dev  -> only dev's pools
    next's pods  select node-class only         -> would match dev's
                                                   pools too, so those
                                                   pools are TAINTED
                                                   and repel them

next therefore keeps emitting exactly the pod spec it emits today, and
the first test below is the one that matters: not "the class is still
there" but "the whole spec is unchanged, key for key".
"""
from __future__ import annotations

import pytest

from kernel.drivers.k8s_job import job_manifest
from kernel.drivers.k8s_spec import (ENV_LABEL, NODE_CLASS_LABEL,
                                     POOL_SCOPE_ENV, node_environment,
                                     node_selector, node_tolerations,
                                     pod_manifest)
from kernel.spec import NODE_CLASSES, ExecutionSpec
from kernel.verifier.prepare_job import prepare_manifest


@pytest.fixture
def scoped(monkeypatch):
    """A cluster whose pools have been migrated."""
    monkeypatch.setenv(POOL_SCOPE_ENV, "on")


@pytest.fixture
def unscoped(monkeypatch):
    """A cluster whose pools have not -- `next`, today."""
    monkeypatch.delenv(POOL_SCOPE_ENV, raising=False)


def _spec(**kw) -> ExecutionSpec:
    base = dict(image="agentos:1", execution_id=7, cpus=2.5, memory_mb=9216)
    base.update(kw)
    return ExecutionSpec(**base)


def _execution(ns: str, **kw) -> dict:
    return pod_manifest(_spec(**kw), "exec-7", namespace=ns,
                        bridge_image="registry/kernel:test",
                        pvc_name="ws-7", service_account="agentos-exec"
                        )["spec"]


def _verification(ns: str, **kw) -> dict:
    return job_manifest(_spec(**kw), "crit-1", namespace=ns, timeout_s=600,
                        service_account="agentos-verifier"
                        )["spec"]["template"]["spec"]


def _prepare(ns: str, **kw) -> dict:
    return prepare_manifest(_spec(**kw), "prep-1", namespace=ns,
                            claim="verify-7", timeout_s=600,
                            service_account="agentos-verifier",
                            kernel_image="registry/kernel:test",
                            source="{}", token="t"
                            )["spec"]["template"]["spec"]


BUILDERS = [pytest.param(_execution, id="execution"),
            pytest.param(_verification, id="verification"),
            pytest.param(_prepare, id="prepare")]


# ── the environment that has NOT been migrated ─────────────────────────

@pytest.mark.parametrize("build", BUILDERS)
def test_an_unmigrated_cluster_gets_exactly_what_it_always_got(
        build, unscoped):
    """THE BACKWARD-COMPATIBILITY GUARANTEE, and the reason the switch
    exists at all.

    `next` is serving real work on pools that carry only the class. This
    asserts the WHOLE selector, not that the class is present -- an
    extra key here is not a cosmetic difference, it is every new pod in
    that environment sitting Pending until someone notices.
    """
    assert build("agentos-staging")["nodeSelector"] == {
        NODE_CLASS_LABEL: "small"}


def test_an_unmigrated_cluster_does_not_even_read_the_namespace(unscoped):
    """A namespace that names no environment is fine while the switch is
    off, because nothing derives anything from it. This is what lets the
    switch be flipped independently of any other cleanup -- E0, a
    laptop and the older fixtures all pass a bare name."""
    assert node_selector("small", "ns") == {NODE_CLASS_LABEL: "small"}


@pytest.mark.parametrize("value", ["off", "OFF", " off ", ""])
def test_off_is_spelled_the_way_the_rest_of_the_kernel_spells_it(
        monkeypatch, value):
    """`off` is the sentinel AGENTOS_EXECUTION_SQL_INSTANCE already uses
    for "this deployment does not have that". Empty counts too: a
    variable set to nothing is a variable nobody filled in."""
    monkeypatch.setenv(POOL_SCOPE_ENV, value)
    assert node_selector("small", "agentos-dev") == {
        NODE_CLASS_LABEL: "small"}


# ── the environment that HAS ───────────────────────────────────────────

@pytest.mark.parametrize("namespace,env", [
    ("agentos-staging", "staging"),
    ("agentos-dev", "dev"),
])
def test_the_environment_comes_from_the_namespace(namespace, env):
    """DERIVED, NOT CONFIGURED. The alternative was a second variable
    naming the environment beside AGENTOS_K8S_NAMESPACE, and two values
    that must agree are two values that eventually do not -- a pod in
    `agentos-dev` selecting next's pool is not an error anything
    reports, it is a story running on the wrong bill."""
    assert node_environment(namespace) == env


@pytest.mark.parametrize("namespace", ["default", "agentos", "agentos-",
                                       "kube-system", ""])
def test_a_namespace_that_names_no_environment_is_refused(namespace):
    """REFUSED, NOT GUESSED, once the switch is on. A wrong value does
    not fail -- the pod is admitted, scheduled against a label no node
    carries, and stays Pending, which `conductor/unscheduled.py` exists
    because nothing reports. The object-building step is the only loud
    place left."""
    with pytest.raises(ValueError) as caught:
        node_environment(namespace)
    assert "agentos-<env>" in str(caught.value)
    assert "Pending" in str(caught.value), (
        "the refusal should say what happens if it guessed instead")


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("env", ["next", "dev"])
def test_every_pod_kind_names_its_environment(build, env, scoped):
    """All three builders, because one that forgot would be the one
    quietly spending the other environment's money -- and the prepare
    Job is the easiest to forget, being the third kind and added last."""
    selector = build(f"agentos-{env}")["nodeSelector"]
    assert selector == {NODE_CLASS_LABEL: "small", ENV_LABEL: env}


@pytest.mark.parametrize("build", BUILDERS)
def test_two_environments_do_not_select_the_same_machines(build, scoped):
    """THE WHOLE POINT, as one assertion. Same class, same spec,
    different environment: the selectors must differ, or both pods
    compete for one set of nodes."""
    assert build("agentos-staging")["nodeSelector"] != \
        build("agentos-dev")["nodeSelector"]


@pytest.mark.parametrize("node_class", NODE_CLASSES)
def test_the_class_survives_the_addition(node_class, scoped):
    """The environment is an ADDITIONAL key, not a replacement. A story
    that asked for a large machine still gets one."""
    assert node_selector(node_class, "agentos-dev") == {
        NODE_CLASS_LABEL: node_class, ENV_LABEL: "dev"}


# ── the taint, which is the half the selector cannot do ────────────────

@pytest.mark.parametrize("build", BUILDERS)
def test_an_unmigrated_cluster_gets_no_toleration_key_at_all(
        build, unscoped):
    """ABSENT, NOT EMPTY. `tolerations: []` behaves identically to
    Kubernetes, and that is not the standard being held to here: the
    pod spec `next` produces must be what it produced before any of
    this existed, key for key."""
    assert "tolerations" not in build("agentos-staging")


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("env", ["next", "dev"])
def test_every_pod_kind_tolerates_its_own_pools_taint(build, env, scoped):
    """All three builders again, and for a sharper reason than the
    selector. A pod that selects a pool it cannot tolerate is repelled
    BY ITS OWN POOL -- Pending, for ever, reporting nothing."""
    assert build(f"agentos-{env}")["tolerations"] == [
        {"key": ENV_LABEL, "operator": "Equal", "value": env,
         "effect": "NoSchedule"}]


@pytest.mark.parametrize("build", BUILDERS)
def test_the_selector_and_the_toleration_are_never_separated(
        build, scoped):
    """THE PAIR IS THE POINT, and each half alone is a distinct fault:

      labelled, not tolerated  -> our own pods cannot use our own pool
      tolerated, not selected  -> our pods still match every other
                                  environment's pool of that class

    So this asserts they arrive together rather than asserting each.
    The way it breaks is a fourth pod builder that remembers one.
    """
    spec = build("agentos-dev")
    assert (ENV_LABEL in spec["nodeSelector"]) is bool(
        spec.get("tolerations"))


def test_the_taint_key_is_the_label_key(scoped):
    """One string, used as a node label and as a taint key. Two
    spellings would be two things to keep in step, and the failure of
    letting them drift is a pool that repels the pods it was created
    for."""
    tolerations = node_tolerations("agentos-dev")
    assert tolerations[0]["key"] == ENV_LABEL
    assert node_selector("small", "agentos-dev")[ENV_LABEL] ==         tolerations[0]["value"]


def test_an_unmigrated_environment_tolerates_nothing(unscoped):
    assert node_tolerations("agentos-staging") == []
