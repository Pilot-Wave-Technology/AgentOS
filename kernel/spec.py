"""ExecutionSpec — substrate-agnostic execution specification and validation.

Defines the contract between the Conductor and any substrate driver
(Docker at C3, Kubernetes at I2). The validator enforces rule 14:
no spec may request host access — no arbitrary host path mounts,
no privileged execution, no host network — and the driver refuses
such a spec before it reaches the substrate API.

This module has NO Docker or Kubernetes imports. Both drivers call
the same validate_spec() function.
"""

from __future__ import annotations

from dataclasses import dataclass, field


_BIND_MODES = {"ro", "rw", "z", "Z", "rshared", "rslave", "rprivate",
               "shared", "slave", "private", "delegated", "cached",
               "consistent"}


def _split_bind(bind: str) -> tuple[str, str]:
    """Return (host_path, container_path) from a Docker bind string.

    Right-to-left, because a Windows host path contains a colon and a
    left-to-right split silently truncates it to the drive letter.
    """
    parts = bind.split(":")
    if len(parts) > 2 and parts[-1] in _BIND_MODES:
        parts = parts[:-1]
    if len(parts) < 2:
        return bind, ""
    return ":".join(parts[:-1]), parts[-1]


class SpecValidationError(Exception):
    """Raised when an ExecutionSpec violates a security constraint."""


# ── Capabilities: words the substrate resolves ───────────────────────────────
#
# WHY WORDS. `runtimeClassName: gvisor` is a Kubernetes noun. Putting it
# here would make every caller that wants a sandbox depend on Kubernetes,
# which is the one thing this module exists to prevent -- it has no
# Docker or Kubernetes import and both drivers share validate_spec().
# A word says what the work NEEDS; the driver decides how its substrate
# spells that, and refuses when it cannot spell it at all.
#
# CLOSED SETS, not free strings. A typo in a free-form isolation string
# is a silently weaker sandbox, which is the failure you find out about
# from someone else.

# How strongly the sandbox is separated from the host kernel.
#   shared   the host kernel, namespace isolation only. What runs today.
#   syscall  a syscall-interception sandbox (gVisor / runsc on GKE).
#   vm       a hardware-virtualised boundary (Kata, Firecracker).
ISOLATION = ("shared", "syscall", "vm")
DEFAULT_ISOLATION = "shared"

# What the workspace disk has to be good at. Not a size -- size is
# `volume_gib`, because a number is already substrate-neutral and a word
# for it would just be a lookup table nobody can read.
#   standard  general-purpose, the default (pd-balanced on GCP)
#   fast      IOPS matter: a large monorepo's index, a heavy test suite
VOLUME_CLASSES = ("standard", "fast")
DEFAULT_VOLUME_CLASS = "standard"

# Which pool of machines this belongs on. A word, so "large" can mean a
# different machine family per cluster without anything above changing.
#
# THE SAME THREE WORDS AS THE SIZE LADDER, and that is the point rather
# than a coincidence. These used to be ("standard", "large", "gpu") while
# kernel/sizes.py offered small/medium/large/xlarge, so the two axes
# could not line up even in principle -- and nothing ever set this, so
# every execution took "standard" and landed wherever the scheduler
# liked. A medium story sharing a machine with two others is the defect
# one-story-per-node exists to remove, and it cannot be removed while
# the tier a story asks for and the machine it lands on are named in
# different languages.
#
# So a tier IS a node class: `spec_builder` sets this from the resolved
# size, the pools in scripts/infra/gke_standard_pools.sh wear these
# words as `agentos.io/node-class` labels, and a story asking for
# `medium` gets a machine sized for exactly one medium story.
#
# "gpu" IS GONE, having never been reachable: no pool ever wore the
# label and no caller ever asked for it. It can come back the day a pool
# exists, which is the honest order.
NODE_CLASSES = ("small", "medium", "large")
DEFAULT_NODE_CLASS = "small"


class CapabilityUnsupported(SpecValidationError):
    """This substrate cannot provide what the spec asked for.

    A SEPARATE EXCEPTION, and a subclass so existing handlers still catch
    it. The distinction it carries is the whole point: SpecValidationError
    means the spec is wrong and no substrate would accept it;
    CapabilityUnsupported means the spec is fine and THIS substrate cannot
    honour it -- which is a scheduling decision, not a bug in the caller.

    IT MUST NEVER BE A SILENT DOWNGRADE. A driver that quietly ran a
    `syscall` spec on the host kernel would hand back a container the
    caller believes is sandboxed. Nothing downstream can tell the
    difference, and the first evidence would be an escape. Refusing by
    name is the only honest answer a substrate can give.
    """

    def __init__(self, field: str, value: str, driver: str,
                 supported) -> None:
        self.field, self.value, self.driver = field, value, driver
        self.supported = tuple(supported)
        super().__init__(
            f"{driver} cannot provide {field}={value!r}; it supports "
            f"{', '.join(self.supported) or 'nothing for this field'}. "
            f"Run this work on a substrate that can, or change the "
            f"requirement -- it will not be silently downgraded.")


def require_supported(spec: "ExecutionSpec", driver_name: str,
                      capabilities: dict) -> None:
    """Refuse a spec this driver cannot honour, by name.

    `capabilities` is {field: (values...)}, declared by the driver. A
    field absent from it means the driver supports only the default --
    stated that way round so a new capability is refused by every
    existing driver until one opts in, rather than silently ignored by
    all of them.
    """
    for field, default in (("isolation", DEFAULT_ISOLATION),
                           ("volume_class", DEFAULT_VOLUME_CLASS),
                           ("node_class", DEFAULT_NODE_CLASS)):
        asked = getattr(spec, field, default)
        allowed = capabilities.get(field, (default,))
        if asked not in allowed:
            raise CapabilityUnsupported(field, asked, driver_name, allowed)

@dataclass
class ExecutionSpec:
    """Substrate-agnostic execution specification.

    Fields map to the intersection of Docker and Kubernetes concepts.
    The driver translates them to substrate-specific API payloads.
    """

    image: str
    command: list[str] = field(default_factory=lambda: ["sleep", "3600"])
    env: dict[str, str] = field(default_factory=dict)
    # THE SANDBOX'S TIMINGS, RESOLVED AT DISPATCH. Three of the timings
    # knobs are enforced inside the sandbox container, which has no
    # database, no DSN and a closed env list -- so they cannot be looked
    # up there and must arrive as literals with the spec.
    #
    # SEPARATE FROM `env`, which `assert_no_credentials` governs and
    # which the worker container carries. These are budgets, they open
    # nothing, and they belong to the container whose closed list the
    # manifest builder reviews by hand.
    #
    # Empty means production, which is also what an unset variable means
    # to the reader in the sandbox -- so a driver that has not learned
    # about this field yet produces a production pod rather than a
    # broken one.
    timings: dict[str, str] = field(default_factory=dict)
    binds: list[str] = field(default_factory=list)
    # A NAMED VOLUME, optionally narrowed to a subpath inside it.
    #
    # `binds` cannot express this: the workspace is a named volume, and
    # `volume_name/sub:/target` is not a bind, it is a volume name with
    # slashes in it -- which the daemon rejects. Docker's Mounts API has
    # carried VolumeOptions.Subpath since API 1.45, and that is the only
    # way to give a sandbox ONE story's directory out of a shared volume.
    #
    # Each entry: (volume_name, container_target, subpath). An empty
    # subpath mounts the whole volume, which is the pre-E7f behaviour.
    volume_mounts: tuple = ()
    network_mode: str = "bridge"
    execution_id: int | None = None  # metadata for container labels; not validated
    # WHICH STORY, and it is not decoration. The git credential broker
    # scopes a token to (story, repo) -- a story holding a repo
    # read-only gets a read-only token -- so a workspace composed
    # without this cannot authenticate at all. On Docker the conductor
    # composes the workspace itself and reads the story straight from
    # the database; in a pod there is no database, so it travels here.
    # Metadata, like execution_id, and not validated.
    story_id: str = ""
    privileged: bool = False
    memory_mb: int = 512

    # F2: the uid the process runs as, "uid:gid" or a name. Empty means
    # the image's default. The Verifier runs as a DIFFERENT uid from the
    # executor deliberately — one of the four properties that make its
    # container trustworthy — so that nothing the executor left owned and
    # writable on the path can be executed by the thing checking it.
    # Maps to Docker's `User` and to a K8s securityContext.runAsUser.
    user: str = ""

    # CPU count. Maps to Docker NanoCpus (cpus * 1e9) and K8s
    # resources.limits.cpu. NOT Docker's CpuQuota (microseconds per
    # period) — that is a different concept. Named to match
    # `docker run --cpus=N`.
    cpus: float = 1.0

    # ── Capabilities. Words; see the vocabulary above. ───────────────
    # Defaults are what runs today, so every existing caller keeps
    # working and no driver has to support anything new to accept an
    # unchanged spec.
    isolation: str = DEFAULT_ISOLATION
    volume_class: str = DEFAULT_VOLUME_CLASS
    node_class: str = DEFAULT_NODE_CLASS

    # The workspace disk, in GiB. A number rather than a word: it is
    # already substrate-neutral, and a word for it would be a lookup
    # table nobody can read at the call site. 0 means "the substrate's
    # own default", which on Docker is the container filesystem.
    volume_gib: int = 0

    # I2 -- WHAT THE WORKSPACE IS MADE OF, carried to the substrate.
    #
    # On Docker the conductor composes the workspace itself and the
    # driver never needs to know; on a pod substrate the composing
    # happens INSIDE the pod, so the mount list has to travel with the
    # spec that builds it. Carried rather than re-derived there:
    # `mounts_for` reads four tables and joins git_nodes to resolve the
    # engine's feature branch, and giving an execution pod the database
    # to ask again would hand it the one credential this design keeps
    # furthest away from it.
    workspace_mounts: tuple = ()

    # Bind-mount allowlist. Empty = all host mounts refused (rule 14).
    # C4 populates this for kernel/; the validator checks each bind's
    # host-side path starts with one of these prefixes.
    allowed_bind_prefixes: list[str] = field(default_factory=list)


def validate_spec(spec: ExecutionSpec) -> None:
    """Validate an ExecutionSpec against rule 14 security constraints.

    Raises SpecValidationError if the spec requests host access.
    Must be called by every driver's create() BEFORE any substrate
    API call — so the refusal happens locally, not at the API.
    """
    # The words have to BE words. A free-form isolation string that
    # nothing checks is a sandbox weaker than the one the caller asked
    # for, discovered later and by someone else.
    for field, allowed in (("isolation", ISOLATION),
                           ("volume_class", VOLUME_CLASSES),
                           ("node_class", NODE_CLASSES)):
        value = getattr(spec, field)
        if value not in allowed:
            raise SpecValidationError(
                f"{field}={value!r} is not one of {', '.join(allowed)}")

    if spec.volume_gib < 0:
        raise SpecValidationError(
            f"volume_gib={spec.volume_gib} is negative")

    if spec.privileged:
        raise SpecValidationError(
            "privileged=True grants root-equivalent access on the host "
            "(rule 14)"
        )

    if spec.network_mode == "host":
        raise SpecValidationError(
            "network_mode='host' bypasses network isolation — "
            "root-equivalent on the host (rule 14)"
        )

    for source, target, subpath in spec.volume_mounts:
        if not source or "/" in source or "\\" in source:
            raise SpecValidationError(
                f"volume mount source {source!r} is not a volume name; a "
                f"path here would be a bind wearing a volume's clothes, "
                f"and rule 14 exists because the proxy cannot tell them "
                f"apart")
        if not target.startswith("/"):
            raise SpecValidationError(
                f"volume mount target {target!r} must be absolute")
        parts = [p for p in (subpath or "").replace("\\", "/").split("/") if p]
        if subpath and (subpath.startswith("/") or ".." in parts):
            raise SpecValidationError(
                f"volume subpath {subpath!r} escapes the volume; a subpath "
                f"is the containment, so one that can climb out of it is "
                f"worse than none at all")

    for bind in spec.binds:
        # Docker bind format: src:dst[:mode].
        #
        # Parsed from the RIGHT, not the left. `split(":")[0]` takes the
        # host path to be everything before the first colon, which on a
        # Windows host turns "C:\repo:/app/bridge:ro" into "C" — and then
        # every legitimate bind is rejected because "C" is under no allowed
        # prefix. The E0 development environment is Docker Desktop on
        # Windows, so this rejected every real spec the conductor could
        # build. The destination is always an absolute POSIX path, which is
        # what makes right-to-left unambiguous.
        host_path, _ = _split_bind(bind)
        if not any(
            host_path.startswith(prefix)
            for prefix in spec.allowed_bind_prefixes
        ):
            if spec.allowed_bind_prefixes:
                raise SpecValidationError(
                    f"bind mount '{bind}' rejected — host path is not in "
                    f"the allowed set (rule 14). "
                    f"Allowed prefixes: {spec.allowed_bind_prefixes}"
                )
            else:
                raise SpecValidationError(
                    f"bind mount '{bind}' rejected — host path is not in "
                    f"the allowed set (rule 14). "
                    f"allowed_bind_prefixes is empty; C4 populates it."
                )
