"""Stage I added five substrate seams. Local execution took none of them.

Every increment in Stage I exists because Kubernetes cannot do something
Docker does — compose a workspace, reach a sandbox on loopback, run git
where the tree is. Each one is a place where local execution could have
been changed to serve the cloud, and the rule was that it must not be:
E0 and a developer's Docker stack keep the code path they already had.

THIS FILE IS THAT RULE, AS A TEST. Not "Docker still works" in general —
the suite covers that — but specifically that every seam DEFAULTS to the
local behaviour, so adding the cloud path could not have moved it. A
default is the cheapest thing in the world to change by accident.

The one deliberate change is recorded at the bottom.
"""
import inspect

from kernel.bridge.client import BRIDGE_HOST, bridge_health, call_bridge
from kernel.bridge.shim import DEFAULT_BIND, _bind_from
from kernel.driver import Driver
from kernel.drivers.docker import DockerDriver
from kernel.envelope import ExecutionEnvelope


# ── the five seams, each defaulting to local ─────────────────────────

def test_the_workspace_is_composed_here_by_default():
    # I2. "pod" means the conductor cannot touch the tree; everything
    # else composes it in process, as it always did.
    assert Driver.WORKSPACE_MODEL == "local"
    assert getattr(DockerDriver, "WORKSPACE_MODEL", "local") == "local"


def test_the_bridge_is_on_loopback_by_default():
    # I1. Docker puts the sandbox in the caller's network namespace, so
    # localhost IS the sandbox and nothing else can reach it.
    assert BRIDGE_HOST == "localhost"
    assert inspect.signature(call_bridge).parameters["host"].default == "localhost"
    assert inspect.signature(bridge_health).parameters["host"].default == "localhost"


def test_the_shim_still_binds_loopback_by_default():
    # I1. Widening this to serve pods would have weakened every existing
    # deployment: under a shared netns 0.0.0.0 exposes the bridge to
    # every container on the network.
    assert DEFAULT_BIND == "127.0.0.1"
    assert _bind_from([]) == "127.0.0.1"


def test_a_namespace_sharing_driver_answers_localhost():
    # I1. Concrete on the base class rather than abstract, so no
    # existing driver had to be edited to keep being right.
    assert "resolve_bridge_host" not in DockerDriver.__dict__


def test_the_disk_is_never_reclaimed_from_a_local_run():
    # I6. Docker's workspace is a directory in a volume the next run
    # reuses; there is no claim, and the base class says so rather than
    # every local driver having to.
    assert Driver.release_workspace.__qualname__.startswith("Driver")


def test_the_tree_is_on_this_filesystem_by_default():
    # I4. Empty is not "unknown", it is "local" -- and it is what every
    # Docker execution passes.
    assert ExecutionEnvelope().workspace_host == ""


def test_the_archive_is_taken_here_by_default():
    # I4. `git archive` is a local subprocess unless the driver says the
    # repository is somewhere this process cannot see.
    #
    # Asserted DIRECTLY, not through getattr with a fallback. It was
    # written that way while ARCHIVE_MODEL did not exist yet, which made
    # it a test that could not fail -- it would have reported the local
    # default correct even if the attribute were never added at all.
    assert Driver.ARCHIVE_MODEL == "local"
    assert "ARCHIVE_MODEL" not in DockerDriver.__dict__


# ── and the local paths are still the ones that run ──────────────────

def test_a_local_workspace_read_never_touches_the_client(monkeypatch):
    from kernel.harness.emit import read_workspace_file

    def _boom(*a, **k):
        raise AssertionError("a local execution called the pod client")

    monkeypatch.setattr("kernel.workspace.podfs_client.read_file", _boom)
    _, problem = read_workspace_file(
        ExecutionEnvelope(workspace_path="/nowhere"), "x.md")
    assert "E_NO_FILE" in problem          # the LOCAL refusal


def test_pod_only_modules_are_not_imported_by_the_local_path():
    # kernel.workspace.podfs pulls in http.server and the whole service.
    # A local execution must not need it loaded to run a leg -- and if
    # it ever does, that is a dependency the E0 container has to carry.
    import kernel.workspace.podfs_client as client

    src = inspect.getsource(client.read_file)
    assert "podfs" not in src or "import" not in src.split("def")[0]


# ── the one deliberate change, stated rather than hidden ─────────────

def test_local_containers_are_bigger_than_they_were_and_that_is_the_point():
    """I7 raised Docker's allocation; one-story-per-node raised it again.

    The size ladder is substrate-neutral -- `build_spec` is shared -- so
    a local container took the small rung with everything else. I7 moved
    it off the dataclass default of 512Mi, which was never a considered
    number and OOMs any real test run, to 4 GiB.

    IT IS 9 GiB AND 2.5 vCPU NOW, because the rungs are no longer chosen
    as allocations at all. They are chosen to fill a machine from the
    pool that shares their name, so that a GKE node holds exactly one
    story (kernel/sizes.py argues this at length). `small` fills an
    e2-standard-4, and the number it takes from that calculation is the
    number Docker is handed on a laptop.

    WHAT THAT ACTUALLY DOES TO E0, since a laptop has neither pools nor
    nodes and the figure is just a cgroup limit:

      * 9 GiB is a CAP, not a reservation. Docker sets it without
        checking the host, and on Docker Desktop -- whose VM typically
        gets half of host RAM -- a limit above the VM's size is simply
        unreachable. The effect locally is a container that behaves as
        it did before, and the risk is the OPPOSITE of a laptop swapping:
        the limit stops limiting.
      * 2.5 vCPU is enforced and fits any machine E0 runs on.
      * `large` at 14 vCPU does NOT fit, and Docker refuses it by name --
        "Range of CPUs is from 0.01 to N". That is a true statement about
        the laptop rather than a false one about the driver, which is
        why kernel/drivers/docker.py accepts all three node classes
        instead of pretending the tier is unavailable.

    Recorded here so the change is a decision somebody can find, not a
    surprise a developer hits when their laptop swaps.
    """
    from kernel.conductor.spec_builder import build_spec
    from kernel.sizes import resolve

    spec = build_spec({"id": 1, "kind": "errand", "domain": "ops",
                       "goal_id": "G"}, [], {})
    assert spec.memory_mb == resolve("small").memory_mb == 9216
    assert spec.cpus == 2.5
    # AND THE LOCAL DEFAULT IS STILL THE SMALLEST RUNG. A default that
    # drifted upward would hand every errand a machine-filling pod.
    assert spec.node_class == "small"
