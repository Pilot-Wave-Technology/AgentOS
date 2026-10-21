"""How big a run is, as a word — and the numbers it stands for.

WHAT WAS TRUE BEFORE. `build_spec` set no size at all. It returned an
`ExecutionSpec` carrying `image`, `command`, `env`, `execution_id` and
`allowed_bind_prefixes`, and nothing else — so every execution took the
dataclass defaults: **1 vCPU, 512Mi, and a 50 GiB disk**. An errand
summarising a branch and a coding story on a monorepo were dispatched
identically, because the only thing that could have told them apart was
never read.

Three costs, all measured on `next`:

  * `_resources()` is applied to BOTH containers of an execution pod, so
    "1 vCPU" bought 2 vCPU of quota. Against a 64-CPU namespace that is
    ~28 concurrent runs, most of it held by workers doing nothing.
  * 512Mi per vCPU is **below Autopilot's memory:CPU floor**, so
    Autopilot silently raises the request and bills the raised figure.
    `_resources`' own docstring warns about this; nothing was in ratio
    to begin with.
  * 50 GiB per execution against a quota of 40 claims is 2 TiB of disk
    for work that mostly needs a shallow clone.

THE TIER SIZES THE SANDBOX, NOT THE POD. The sandbox is where the work
happens — installs, builds, test runs. The worker runs git, file reads
and an HTTP service, and needs the same allocation on every tier. Sizing
them together is what made small runs expensive: the worker was given
whatever the sandbox got, and never used it.

So `ExecutionSpec.cpus` and `.memory_mb` mean THE SANDBOX throughout.
On Docker that is the only container there is, so the meaning is
unchanged; on Kubernetes the worker is given `WORKER_*` beside it.

ONE STORY PER MACHINE, WHICH IS WHAT THESE NUMBERS NOW MEAN. The rungs
used to be chosen for their ratio and packed several to a node; they are
chosen now to very nearly FILL one machine from the pool that shares
their name, so a story gets a machine and the machine gets one story.
`small` fills an e2-standard-4, `medium` an e2-standard-8, `large` an
e2-standard-16. The pools are in scripts/infra/gke_standard_pools.sh and
wear these words as labels; kernel/spec.py explains why the tier and the
node class are the same word.

WHY EACH ROW STOPS SHORT OF THE MACHINE. A node does not offer what its
spec sheet says. GKE reserves CPU and memory for the kubelet and the
system, on a sliding scale that takes proportionally MORE memory from a
big machine -- an e2-standard-16 advertises 64 GiB and allocates about
58.4. Then the DaemonSets take their cut: fluentbit, the metrics agent,
the metadata server, the CSI node driver. And the pod is not the sandbox
alone; the worker adds 0.5 vCPU and 2 GiB and the cloud-sql-proxy
sidecar another 0.25 and 512Mi.

Rows are sized so that sandbox + worker + sidecar still fits with room
to spare, because THE FAILURE MODE IS PENDING. A pod that asks for
slightly more than any node can offer is not rejected and does not fail;
it waits, forever, while the autoscaler adds a machine that is also too
small and the story looks merely slow. The margin below is the
difference between a bad afternoon and a working fleet:

    tier    sandbox        pod total       node            allocatable
    small   2.5 / 9 GiB    3.25 / 11.5     e2-standard-4   3.92 / 13.3
    medium  6.5 / 23 GiB   7.25 / 25.5     e2-standard-8   7.91 / 28.3
    large   14.0 / 52 GiB  14.75 / 54.5    e2-standard-16  15.89 / 58.4

`large` is 14.0 rather than the 14.5 the build plan proposed, and 52 GiB
rather than 54. At 14.5/54 the pod totals 15.25 and 56.5 against 15.89
and 58.4 -- which fits on paper and leaves under half a core and under
two GiB for every DaemonSet on the node. The half core buys nothing a
story can feel; being certain the pod schedules buys a great deal.

THE RATIO IS NO LONGER 1:4, and that is a consequence rather than a
choice. e2-standard machines are exactly 4 GiB per vCPU, but GKE's
reservation takes a larger share of the memory than of the CPU, so a row
that fills a machine comes out near 1:3.6. That is still well inside the
1:1 to 6.5:1 band Autopilot enforces, which matters only for as long as
anything still runs there.
"""

from __future__ import annotations

from dataclasses import dataclass

# The worker's allocation, identical on every tier. It clones, commits,
# archives, reads files for the explorer and answers HTTP; none of that
# scales with the size of the work. 0.5 vCPU to 2 GiB keeps the 1:4
# ratio so the pod total stays in ratio however the tiers are mixed.
WORKER_CPUS = 0.5
WORKER_MEMORY_MB = 2048


@dataclass(frozen=True)
class Size:
    """One rung. `cpus`/`memory_mb` are the SANDBOX's."""

    name: str
    cpus: float
    memory_mb: int
    volume_gib: int

    @property
    def pod_cpus(self) -> float:
        """What the pod actually costs, worker included.

        Reported because the tier's own number is the misleading one:
        the quota is spent per POD, and a reader comparing `cpus`
        against a 64-CPU ceiling is out by the worker on every row.
        """
        return self.cpus + WORKER_CPUS

    @property
    def pod_memory_mb(self) -> int:
        return self.memory_mb + WORKER_MEMORY_MB


# The ladder. Disks climb with the tiers because the thing that fills a
# disk — dependencies, build output, a deep history — is the same thing
# that wants the CPU.
# `xlarge` IS GONE. It named a fourth rung with no fourth pool, and
# nothing ever admitted at it -- two tests describe it as the size that
# has never run. A tier that cannot be placed is a promise the fleet
# cannot keep; the day a pool exists to hold it, it comes back.
SIZES: dict[str, Size] = {
    "small":  Size("small",  cpus=2.5,  memory_mb=9216,  volume_gib=10),
    "medium": Size("medium", cpus=6.5,  memory_mb=23552, volume_gib=30),
    "large":  Size("large",  cpus=14.0, memory_mb=53248, volume_gib=50),
}

# EVERYTHING IS SMALL UNTIL SOMEBODY SAYS OTHERWISE. A default that is
# generous is a default nobody revisits, and the quota is shared with
# production. A run that genuinely needs more says so on its profile,
# where the claim is visible and reviewable, rather than inheriting it
# from a number in a dataclass.
DEFAULT_SIZE = "small"


class UnknownSize(ValueError):
    """The word is not a rung. Never silently resolved to a default.

    A typo that resolves to `small` gives a large story a small pod and
    a failure that looks like the work — an OOM in a test run reads as
    a broken test. Refusing names the actual problem at the point the
    profile is loaded.
    """


def resolve(name: str | None) -> Size:
    """The rung `name` stands for. Empty or None means the default."""
    key = (name or "").strip().lower() or DEFAULT_SIZE
    if key not in SIZES:
        raise UnknownSize(
            f"{name!r} is not a size; expected one of "
            f"{', '.join(sorted(SIZES))} (default {DEFAULT_SIZE})")
    return SIZES[key]
