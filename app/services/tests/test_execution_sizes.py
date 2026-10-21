"""A run is sized, and by default it is small.

`build_spec` used to set no size at all -- it returned a spec carrying
image, command, env, execution_id and allowed_bind_prefixes, so every
execution took the dataclass defaults of 1 vCPU, 512Mi and a 50 GiB
disk. An errand summarising a branch and a coding story on a monorepo
were dispatched identically, and the profile that knew the difference
was never asked.

Three consequences these tests pin:
  * the rung reaches the spec at all,
  * the two containers are NOT the same size (the pod used to cost 2x
    the tier because `_resources` took the spec alone and ran twice),
  * every rung stays inside Autopilot's memory:CPU band, so a request
    is billed as asked rather than silently raised.
"""
import pytest

from kernel.conductor.spec_builder import build_spec
from kernel.drivers.k8s_spec import _resources, pod_manifest
from kernel.sizes import (DEFAULT_SIZE, SIZES, WORKER_CPUS, WORKER_MEMORY_MB,
                          UnknownSize, resolve)

ROW = {"id": 1, "kind": "errand", "domain": "ops", "goal_id": "G"}


# ── the ladder itself ────────────────────────────────────────────────

def test_the_default_is_small():
    assert DEFAULT_SIZE == "small"
    assert resolve(None).name == "small"
    assert resolve("").name == "small"


def test_every_rung_is_in_autopilot_ratio():
    # Autopilot accepts 1:1 to 6.5:1 GiB per vCPU and REWRITES anything
    # outside it, billing the rewrite. A rung out of band is a rung
    # nobody is actually running.
    for size in SIZES.values():
        gib_per_cpu = (size.memory_mb / 1024) / size.cpus
        assert 1.0 <= gib_per_cpu <= 6.5, f"{size.name} is 1:{gib_per_cpu}"


def test_the_pod_total_includes_the_worker():
    # The tier's own number is the misleading one: quota is spent per
    # POD, so a reader comparing `cpus` against a 64-CPU ceiling is out
    # by the worker on every row.
    small = resolve("small")
    assert small.pod_cpus == small.cpus + WORKER_CPUS
    assert small.pod_memory_mb == small.memory_mb + WORKER_MEMORY_MB


def test_the_rungs_climb_on_both_axes():
    # THREE, not four. `xlarge` named a rung with no pool to place it
    # on; kernel/sizes.py has the argument for why a tier that cannot be
    # scheduled is worse than a tier that does not exist.
    order = ["small", "medium", "large"]
    assert list(SIZES) == order, "the ladder is these three, in this order"
    cpus = [SIZES[n].cpus for n in order]
    disks = [SIZES[n].volume_gib for n in order]
    assert cpus == sorted(cpus) and len(set(cpus)) == 3
    assert disks == sorted(disks) and len(set(disks)) == 3


def test_every_rung_is_a_pool_and_every_pool_is_a_rung():
    """The two vocabularies are one vocabulary, and this is the wire.

    `spec_builder` sets `node_class=size.name`, so a rung whose name is
    not a node class would build a spec that `validate_spec` refuses --
    at dispatch, for every execution at that tier, with a message about
    an invalid capability rather than about the rung that was renamed.
    """
    from kernel.spec import NODE_CLASSES

    assert set(SIZES) == set(NODE_CLASSES)


def test_a_rung_leaves_room_for_the_machine_it_fills():
    """Because the failure mode is Pending, not failed.

    A pod asking for marginally more than any node can offer is not
    rejected: it waits while the autoscaler adds another machine that is
    also too small. These are GKE's allocatable figures for the pool
    each rung is sized against, less a deliberately generous budget for
    the DaemonSets that land on every node.
    """
    # (allocatable cpu, allocatable GiB) for e2-standard-4 / -8 / -16,
    # less 0.4 vCPU and 1.5 GiB of DaemonSets.
    room = {"small": (3.52, 11.8),
            "medium": (7.51, 26.8),
            "large": (15.49, 56.9)}
    # The sqlproxy sidecar rides in every execution pod (plan 11.5a).
    sqlproxy_cpus, sqlproxy_gib = 0.25, 0.5
    for name, (cpu_room, gib_room) in room.items():
        rung = SIZES[name]
        pod_cpus = rung.pod_cpus + sqlproxy_cpus
        pod_gib = rung.pod_memory_mb / 1024 + sqlproxy_gib
        assert pod_cpus <= cpu_room, (
            f"{name} asks {pod_cpus} vCPU of {cpu_room} available: it "
            f"would sit Pending rather than fail")
        assert pod_gib <= gib_room, (
            f"{name} asks {pod_gib:.1f} GiB of {gib_room} available: it "
            f"would sit Pending rather than fail")


def test_an_unknown_rung_is_refused_not_defaulted():
    # Resolving a typo to `small` gives a large story a small pod, and
    # the OOM that follows reads as a broken test.
    with pytest.raises(UnknownSize) as e:
        resolve("smal")
    assert "small" in str(e.value)


# ── it reaches the spec ──────────────────────────────────────────────

def test_a_profile_with_no_size_dispatches_small():
    spec = build_spec(ROW, [], {})
    small = resolve("small")
    assert (spec.cpus, spec.memory_mb, spec.volume_gib) == (
        small.cpus, small.memory_mb, small.volume_gib)


def test_the_spec_no_longer_takes_the_dataclass_defaults():
    # The defect, stated as a test: 512Mi and a 50 GiB disk were what
    # every execution got because nothing set them.
    spec = build_spec(ROW, [], {})
    assert spec.memory_mb != 512
    assert spec.volume_gib != 0


def test_a_contract_may_ask_for_a_bigger_rung():
    spec = build_spec(ROW, [], {}, contract_row={"size": "large"})
    large = resolve("large")
    assert (spec.cpus, spec.memory_mb, spec.volume_gib) == (
        large.cpus, large.memory_mb, large.volume_gib)


def test_a_contract_naming_an_unknown_rung_is_refused_at_dispatch():
    with pytest.raises(UnknownSize):
        build_spec(ROW, [], {}, contract_row={"size": "enormous"})


# ── the two containers differ ────────────────────────────────────────

def test_the_worker_does_not_get_the_sandbox_allocation():
    spec = build_spec(ROW, [], {})
    worker = _resources(spec, sandbox=False)
    sandbox = _resources(spec, sandbox=True)
    assert worker != sandbox
    assert worker["requests"]["cpu"] == str(WORKER_CPUS)
    assert sandbox["requests"]["cpu"] == str(spec.cpus)


def test_the_rendered_pod_costs_the_tier_plus_the_worker():
    # The regression this guards: `_resources(spec)` called twice made a
    # 1 vCPU tier reserve 2 vCPU, halving how many runs fit the quota.
    spec = build_spec(ROW, [], {})
    pod = pod_manifest(spec, "exec-1", namespace="ns", pvc_name="ws-exec-1",
                       service_account="sa",
                       bridge_image="registry/kernel:abc")
    by_name = {c["name"]: c for c in pod["spec"]["containers"]}
    total = sum(float(c["resources"]["requests"]["cpu"])
                for c in by_name.values())
    # 2.5 sandbox + 0.5 worker. The literal is here so that retuning the
    # ladder has to come past this line and say so.
    assert total == resolve("small").pod_cpus == 3.0


def test_requests_and_limits_agree_because_autopilot_bills_the_request():
    spec = build_spec(ROW, [], {})
    for sandbox in (True, False):
        r = _resources(spec, sandbox=sandbox)
        assert r["requests"] == r["limits"]
