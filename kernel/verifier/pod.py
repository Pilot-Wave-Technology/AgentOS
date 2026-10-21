"""The pod the criteria run in: warm, offline, and sized by its tier.

WHY A POD AND NOT A JOB. A Job runs to completion and a warm pod by
definition does not. Plan 5.4 step 9a is the reason: a refusal steers
the executor and the SAME pod checks generation two, with only the tree
re-seeded. A Job's controller would tear the container down between
rounds and the `.venv` -- the expensive half, and the half
`volume.STAMP` exists to keep -- would be rebuilt every time somebody
was refused.

The cost of having no controller is stated in `kernel/ownership.py`: a
pod nothing else owns classifies as `KERNEL`, which means reapable, and
that is intended rather than tolerated. It carries `execution-id` so
`reclaim` can decide from the execution ROW -- the one reaper W1 found
already gets ownership right -- rather than from a label guess.

ZERO EGRESS, AND THAT IS THE WHOLE REASON THERE ARE TWO PODS. F2d: a
check that can reach the internet can fetch a version of itself that
passes, and can post the tree it was handed somewhere. NetworkPolicy on
Autopilot selects PODS and never containers -- plan 5.0 lists the four
per-container escapes that were tried and do not exist -- so an install,
which needs a network, cannot share a pod with a criterion, which must
have none. This manifest therefore carries NO `agentos.io/egress`
label: the namespace is default-deny and that absence IS the denial.
`verification-pod-isolated` in deploy/k8s/conductor.yaml says it out
loud anyway, because a default relied on silently is a default somebody
changes.

SIZED FROM THE TIER, which `kinds.container_spec` is not. It hardcodes
`memory_mb=1024` on every rung, so an `xlarge` story gets 30720Mi to DO
the work and 1024Mi to CHECK it. The checks OOM; an OOM is
`infra_error` and therefore `error` rather than `fail` -- correctly,
since a ceiling nobody declared says nothing about the work -- so it is
three retries and then back to the executor, which is asked to repair
code that was never broken. That is the loop measured on 205, 207, 208
and 209 (plan 9.6), arriving structurally rather than as a bug, and one
lookup in `kernel/sizes.py` is all it ever needed.

THE IMAGE IS THE CONTRACT'S -- never the conductor's, never the
executor's. Defence 1 of plan 5.12: a criterion that inherited the
executor's PATH would find whatever shim the model arranged there, and
F4's second defence is gone. The only kernel bytes in this pod are the
bridge shim, delivered read-only into a scratch volume by an
initContainer; `kernel/drivers/k8s_bridge.py` makes that argument in
full for the execution pod and it is unchanged here.

AND IT HOLDS NOTHING WORTH STEALING. No model key, no database
credential, no forge token, no proxy address -- adjudication happens in
the verifier Deployment (plan 5.1), which is the same split as the
pre-D executor: the conductor judges, the pod does.
"""

from __future__ import annotations

from kernel.drivers import k8s_bridge
from kernel.drivers.k8s_spec import STORAGE_CLASS
from kernel.ownership import KERNEL, MANAGED_BY_LABEL, OWNER_LABEL
from kernel.sizes import DEFAULT_SIZE, Size, resolve
from kernel.verifier import volume
from kernel.verifier.kinds import VERIFIER_UID

# What the NetworkPolicies and the reapers select this class of pod on.
# Not `agentos-execution`: that label is spelled in `execution-egress-to-sql`
# and in `execution-bridge-ingress`, and inheriting it would hand a
# criterion the executor's route to Cloud SQL.
APP = "agentos-verification"
APP_LABEL = "app"

# The shim's own default, and deliberately the same number as the
# sandbox's. The port is not what separates these two pods -- the
# podSelector is -- and a second port would be a second thing to keep
# in step with `kernel/bridge/tools_core.py` for no gain.
BRIDGE_PORT = 9100

# NOT `ws-exec-<id>`, which `ownership.workspace_execution` parses and
# `reclaim` deletes as the EXECUTION's workspace. Two claims per
# execution now exist and they have different lifetimes: the workspace
# survives a preempted executor, and this one survives a manifest that
# did not change. A shared prefix would have the workspace sweep
# collecting an environment mid-verification.
CLAIM_PREFIX = "verify-ws-"
POD_PREFIX = "verify-"

# WHO OWNS THE BYTES ON THE VOLUME, and this is a contract between two
# pods that never meet: the prepare Job writes `.venv` and this pod
# executes out of it. `kinds.VERIFIER_UID` rather than a second literal
# 65534 -- the Verifier running as a uid the executor could not have
# arranged to own anything as is one of the properties that makes a
# check's answer worth having, and a uid spelled twice is a uid that
# disagrees once. `volume.py` declares the paths; this declares who may
# write them.
UID = VERIFIER_UID

# fsGroup, so the claim is chowned on mount and neither the prepare Job
# nor this pod needs to run as root to arrange it -- which they could
# not anyway, `runAsNonRoot` being set on both.
GID = VERIFIER_UID


def pod_name(execution_id) -> str:
    return f"{POD_PREFIX}{execution_id}"


def claim_name(execution_id) -> str:
    return f"{CLAIM_PREFIX}{execution_id}"


def size_for(contract: dict | None) -> Size:
    """The rung this contract's checks run on.

    THE TIER IS ALREADY THE DECLARATION. `contract_config.overrides_from`
    resolves `contract.size` at dispatch and refuses an unknown word
    there, so a monorepo that asked for `large` to build has said, in
    the one place a person reviews, how much it needs to be checked in
    too. Inventing a second field would let the two disagree, and the
    disagreement would surface as an OOM in a test run -- which reads
    as a broken test.

    An absent contract is the default rung rather than an error: a
    profile with no contract has nothing to verify, and this is also
    what the fixtures build.
    """
    return resolve((contract or {}).get("size") or DEFAULT_SIZE)


class ImageMissing(RuntimeError):
    """The contract names no image, so there is nothing to check in."""


def image_for(contract: dict | None) -> str:
    """The image the checks run in, and it is the CONTRACT's.

    DIGEST BEFORE TAG, the same order `contract_config.overrides_from`
    and `kinds.run_command` use -- F6's parity check exists because a
    contract naming an image by tag may not be the image the Verifier
    ran, and every run on 2026-09-17 logged exactly that (plan 5.2, E6).

    NEVER `AGENTOS_EXECUTION_IMAGE` and never the executor's sandbox
    image. Defence 1: a surviving PATH shim makes F4 meaningless, and
    the cheapest way to inherit one is to reach for whichever image is
    already in hand. Refused by name rather than defaulted, because a
    pod built on the wrong image still starts and still answers -- with
    results that mean nothing.
    """
    image = (contract or {}).get("base_digest") or \
        (contract or {}).get("base_image")
    if not image:
        raise ImageMissing(
            "the contract names no base image, so there is no "
            "environment to run its checks in. The verification pod "
            "takes the CONTRACT's image and never the executor's -- see "
            "plan 5.12, defence 1.")
    return image


def labels_for(execution_id, extra: dict | None = None) -> dict:
    """Ours, the class, and the execution that owns it.

    `execution-id` is what makes plan 5.7's three triggers expressible
    at all -- terminal, manifest change, idle -- and its absence is what
    let `orphan_sweep` destroy a live capture Job on execution 208. A
    pod this file creates always knows its execution, so unlike
    `k8s_spec.pod_manifest` there is no branch for the id being None.
    """
    return {APP_LABEL: APP, MANAGED_BY_LABEL: KERNEL,
            OWNER_LABEL: str(execution_id), **(extra or {})}


def pvc_manifest(execution_id, *, namespace: str, size: Size,
                 volume_class: str = "standard") -> dict:
    """The tree and the environment, on one disk.

    ReadWriteOncePod, and here it is the SEQUENCER rather than a
    precaution. A GCE disk takes one attacher, so plan 5.5's ~30s
    detach/attach is unavoidable; RWOP makes the API server refuse the
    overlap instead of letting a criterion read a `.venv` the prepare
    Job is halfway through writing. RWO would permit exactly that
    whenever the two landed on one node.

    Sized from the same rung as the executor's disk. The thing that
    fills a verification volume -- dependencies, build output, a large
    tree -- is the thing that filled the executor's, so a second ladder
    would only be a second number to get wrong.
    """
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": claim_name(execution_id), "namespace": namespace,
                     "labels": labels_for(execution_id)},
        "spec": {
            "accessModes": ["ReadWriteOncePod"],
            "storageClassName": STORAGE_CLASS.get(volume_class,
                                                  STORAGE_CLASS["standard"]),
            "resources": {"requests": {"storage": f"{size.volume_gib}Gi"}},
        },
    }


def _command() -> list[str]:
    """Make the tree directory exist, THEN serve.

    `tools_core` resolves WORKSPACE once at import and falls back to
    `/tmp` when the directory is not there -- a guard written for a
    sandbox whose workspace is composed before it starts. This pod is
    warm BEFORE its tree is seeded, so without the mkdir the shim comes
    up rooted at /tmp and every later check runs in an empty directory
    and passes or fails on nothing. Silently: the pod is Ready and the
    bridge answers.

    `exec` so the shim is pid 1 and a SIGTERM reaches it rather than the
    shell.
    """
    return ["sh", "-c",
            f"mkdir -p {volume.TREE} {volume.ENV} && exec "
            + " ".join(k8s_bridge.sandbox_command())]


def _container(image: str, size: Size) -> dict:
    return {
        "name": "checks",
        "image": image,
        "command": _command(),
        # A CLOSED LIST, and the same rule the sandbox's env follows:
        # nothing here opens anything. WORKSPACE is where `bash` runs
        # and where `grep`/`list_dir` are rooted; BRIDGE_PORT is the
        # port. The contract's declared secrets are NOT here -- see the
        # module docstring on what this pod holds.
        "env": [{"name": "BRIDGE_PORT", "value": str(BRIDGE_PORT)},
                {"name": "WORKSPACE", "value": volume.TREE}],
        "volumeMounts": [{"name": "verify", "mountPath": volume.MOUNT},
                         k8s_bridge.sandbox_mount()],
        "resources": {
            "requests": {"cpu": str(size.cpus),
                         "memory": f"{size.memory_mb}Mi"},
            "limits": {"cpu": str(size.cpus),
                       "memory": f"{size.memory_mb}Mi"},
        },
        # WHAT `Ready` HAS TO MEAN. `await_ready` says plainly that "a
        # pod is Running the moment its containers start, and the bridge
        # shim is not listening yet" -- and then reads
        # `container_statuses[].ready`, which without a probe is only
        # "started". The execution pod gets away with it because two
        # init containers run first; this one has almost nothing to do
        # before the shim binds, so the race is tight and the loss is a
        # tool call to a closed port reported as a hung check.
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": BRIDGE_PORT},
            "periodSeconds": 2, "failureThreshold": 30,
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": UID,
        },
        # The shim dies with a message rather than a status code; without
        # this the pod status says `Error` and the reason is in logs the
        # pod may not outlive.
        "terminationMessagePolicy": "FallbackToLogsOnError",
    }


def pod_manifest(execution_id, *, namespace: str, image: str,
                 bridge_image: str, service_account: str, size: Size,
                 labels: dict | None = None) -> dict:
    """The warm pod for one execution's verification.

    `image` is THE CONTRACT'S -- `base_digest` before `base_image`, the
    caller's choice, and never `bridge_image`, which is the kernel's and
    is here only to deliver the shim.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name(execution_id), "namespace": namespace,
                     "labels": labels_for(execution_id, labels)},
        "spec": {
            # ALWAYS, and this is the one place it differs from the
            # execution pod's `Never`. A crashed shim in a Job-less pod
            # is otherwise a verification that can never be run again
            # for this execution -- the PVC survives and nothing brings
            # the listener back. Restarting in place keeps the `.venv`
            # and costs a container start; plan 5.8 row 3 covers the
            # case a restart cannot fix, where the POD is gone.
            "restartPolicy": "Always",
            # THE VERIFIER'S IDENTITY, and with zero egress it is inert:
            # Workload Identity is minted at 169.254.169.254, which this
            # pod may not reach. Named anyway, because
            # `serviceAccountName` is a POD field and the default is the
            # namespace's `default` KSA -- so leaving it off would mean
            # the one thing this pod must not be is whatever somebody
            # binds to `default` later.
            "serviceAccountName": service_account,
            "securityContext": {"fsGroup": GID},
            # 15 seconds is what Spot actually gives, best effort, and a
            # larger number is truncated rather than honoured.
            "terminationGracePeriodSeconds": 15,
            "volumes": [{"name": "verify",
                         "persistentVolumeClaim": {
                             "claimName": claim_name(execution_id)}},
                        k8s_bridge.volume()],
            # Before anything runs, so a pod whose shim could not be
            # delivered never becomes Ready rather than answering
            # nothing later.
            "initContainers": [k8s_bridge.init_container(bridge_image,
                                                         uid=UID)],
            "containers": [_container(image, size)],
        },
    }
