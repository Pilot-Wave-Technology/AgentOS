"""The prepare Job as a manifest. No SDK, no cluster, assertable.

TWO CONTAINERS AND THEY ARE TWO TRUST LEVELS. An initContainer from the
KERNEL's image pulls the committed tree onto the volume (E15,
`prepare_fetch`); the work container from the CONTRACT's image installs
against it. The token that buys the tree is set on the first and not the
second, so the repository's own `.agentos/setup.sh` -- the one piece of
model-adjacent code in this Job -- cannot read a workspace credential
out of its own environment.

Every field below is one of the reasons section 5 replaced the capture
path.

  the CONTRACT's image      defence 1, and 5.12 names it first. Never
                            the executor's: a PATH shim that survives
                            into the thing checking the work makes F4
                            meaningless. `spec.image` is the contract's
                            and this file never substitutes another.
  `egress: prepare`         its OWN class, and it needs one because no
                            other has the pair. The full port 3128, for
                            the package indexes its whole purpose is
                            fetching from, AND an execution pod's podfs
                            on 9101 for the committed tree.
                            NOT `proxy`: that class also selects the
                            EXECUTION pod, whose sandbox runs
                            model-authored code, and the 9101 rule there
                            would let a story dial other stories'
                            workspaces. NOT `capture` either, and the
                            reason is the one this file argued before
                            the class existed: `capture` carries the
                            METADATA SERVER for a sidecar calling
                            `storage.Client()`, and this Job has no
                            sidecar, no artifact store and no cloud
                            identity to mint -- so granting it metadata
                            would hand a credential path to the one
                            container here that runs a repository's own
                            `.agentos/setup.sh`. Faults 8 and 9 have
                            nothing left to fail at.
  `runAsUser` 65534         kinds.VERIFIER_UID, the SAME uid the
                            verification pod runs criteria as, so the
                            `.venv` this writes is owned by the thing
                            that executes it. `pod.py` states the other
                            half of that contract, which is why neither
                            file spells 65534 a second time.
                            NON-ROOT IS PROVEN ON THIS IMAGE, not
                            hoped: `corepack enable` is the one step
                            that looks like it wants root -- it rewrites
                            the pnpm/yarn shims the Dockerfile already
                            wrote -- and the EXECUTOR's sandbox has been
                            running the identical REGISTRY command as
                            uid 65532 (k8s_spec.SANDBOX_UID) all along.
                            Same registry, same image, same command:
                            5.3's whole point is that both sides run it.
  `fsGroup` on the POD      a fresh claim is root-owned and mode 755, so
                            a non-root install could not create
                            `volume.ENV` on it at all. fsGroup chowns
                            the volume on mount, and the tar path's
                            `restamp` -- rewriting every uid in a
                            captured archive -- stops being needed.
  `backoffLimit: 0`         one honest answer. A silent retry would
                            hide a manifest that fails intermittently
                            behind a second attempt, and 5.8 row 5 says
                            a genuinely broken manifest IS about the
                            work and the executor should hear it.
  `execution-id`            on the POD TEMPLATE, which is what Cloud
                            Logging indexes as
                            `labels."k8s-pod/execution-id"`, and with NO
                            None branch (plan 13.2). A pod that cannot
                            name its execution is a pod whose logs are
                            found by luck, and it is also what stopped
                            `orphan_sweep` destroying a live install --
                            it killed one seven seconds in on execution
                            208 for exactly this omission.

THE NETWORK THIS JOB NEEDS, AND BOTH SIDES NOW ADMIT IT. The fetch dials
the EXECUTION pod's workspace surface on 9101, so three policies in
deploy/k8s/conductor.yaml agree: `prepare-egress` lets this class out to
`app: agentos-execution` on 9101 and to the proxy on 3128,
`egress-proxy-ingress` admits the class on 3128, and
`execution-bridge-ingress` admits `app: agentos-verifier` on 9101 ONLY
-- never 9100, which is the sandbox's bridge and runs `bash`.
Unadmitted would be a HANG and not a refusal -- the namespace is
default-deny and a drop does not reset -- so it would read as a slow
install rather than a missing line of YAML.
`app: agentos-verification` must NOT be added to that ingress (plan
5.15a): the criteria pod holds no token and opens nothing.

THE TTL IS SHORT, AND THE REASON IS RWO. A GCE disk takes one attacher
and the verification pod attaches next (5.5, E4). A finished Job's pod
still holds the attachment until it is DELETED -- `run_job` does that in
a `finally`, so this is only the backstop for a verifier that died
between the Job finishing and the delete. Five minutes of backstop, the
criterion Job's number, would be five minutes of a pod that cannot
start.
"""

from __future__ import annotations

from kernel.drivers.k8s_job import MAX_DEADLINE_S
from kernel.spec import ExecutionSpec
from kernel.verifier import prepare_source, volume
from kernel.verifier.kinds import VERIFIER_UID

# The container whose exit status IS the result. `run_job` needs the
# name to read the right log, and there is only one work container here.
PREPARE = "prepare"

# The one that runs first. Named so a reader of `kubectl describe` --
# and of the Cloud Logging query in plan 13.1, which returns a line per
# container -- can tell "the tree never arrived" from "the install
# failed" without reading either log.
FETCH = "tree-fetch"

# See the docstring: a backstop measured against a disk that cannot be
# attached twice, not against how long the evidence is interesting.
TTL_AFTER_FINISHED_S = 30

# The fetch's own allocation. It streams a tar through a 1MB buffer and
# unpacks it member by member, so memory is flat whatever the tree
# weighs -- but the UNPACK is per-file syscalls on a tree that can hold
# a hundred thousand of them, and starving it would spend the RWO
# handover window (5.5) doing nothing. A whole CPU, and the same 1:2
# ratio Autopilot would otherwise rewrite.
FETCH_CPUS = "1"
FETCH_MEMORY_MB = 2048


class ExecutionUnnamed(RuntimeError):
    """A prepare Job with no execution id. Refused before it is created.

    Plan 13.2 makes the label non-negotiable, and the refusal is here
    rather than a `None` branch because the two costs are asymmetric: a
    Job that cannot be correlated is evidence found by luck, and one
    `orphan_sweep` cannot match to an execution is a live install
    destroyed mid-run.
    """


def _resources(cpus, memory_mb) -> dict:
    ask = {"cpu": str(cpus), "memory": f"{memory_mb}Mi"}
    return {"requests": dict(ask), "limits": dict(ask)}


def tree_container(image: str, source: str, token: str) -> dict:
    """The initContainer that puts the committed tree on the volume.

    `image` is the KERNEL's -- it is the only one carrying
    `kernel/verifier/prepare_fetch.py` -- and it is NOT the image the
    install runs in. That is the whole separation: the contract's image
    stays the contract's business (k8s_bridge argues it in full), and
    the workspace token never enters it.
    """
    return {
        "name": FETCH,
        "image": image,
        "command": ["python", "-m", "kernel.verifier.prepare_fetch"],
        "env": [{"name": prepare_source.TREE_VAR, "value": source},
                {"name": prepare_source.TOKEN_VAR, "value": token}],
        "volumeMounts": [{"name": "verify", "mountPath": volume.MOUNT}],
        "resources": _resources(FETCH_CPUS, FETCH_MEMORY_MB),
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            # The SAME uid as the install and as the criteria. A tree
            # unpacked as anyone else is a tree the next container
            # cannot write `__pycache__` into, which fails a pytest run
            # for a reason that is not the work.
            "runAsUser": VERIFIER_UID,
        },
        # The fetch dies with a sentence rather than a status code;
        # without this the pod status says `Error` and the sentence is
        # in logs the pod may not outlive.
        "terminationMessagePolicy": "FallbackToLogsOnError",
    }


def prepare_manifest(spec: ExecutionSpec, name: str, *, namespace: str,
                     claim: str, service_account: str, timeout_s: float,
                     kernel_image: str, source: str, token: str,
                     labels: dict | None = None) -> dict:
    """One prepare run, as a Job that writes onto `claim`."""
    # `capture` AND NOT `proxy`, for the reason VIA_CAPTURE exists.
    #
    # This Job needs one rule `proxy` does not have: egress to an
    # execution pod's podfs on 9101, so its init container can stream
    # the committed tree. `workload-egress-via-proxy` ALSO SELECTS THE
    # EXECUTION POD, whose sandbox runs model-authored code -- granting
    # that rule there would let a story's own code dial other stories'
    # podfs. NetworkPolicy is per-pod, so the only way to give the rule
    # to one and not the other is a separate class, which is the same
    # argument that produced `capture` after execution 205.
    from kernel.drivers.k8s_egress import LABEL, VIA_PREPARE  # noqa: PLC0415

    if spec.execution_id is None:
        raise ExecutionUnnamed(
            "a prepare Job must carry `execution-id` on its pod "
            "template: it is what Cloud Logging indexes, what "
            "`orphan_sweep` spares a live install by, and what plan "
            "5.7's three reap triggers are expressed in.")
    deadline = max(1, min(int(timeout_s), MAX_DEADLINE_S))
    # `agentos.io/kind: verification`, AND THIS ONE WAS MISSED ONCE.
    # A prepare Job asks for the FULL TIER -- `_resources(spec.cpus,
    # spec.memory_mb)` below -- because it runs the same dependency
    # install the story ran, and an install starved of CPU is the
    # slowest thing in a verification. While the tiers were small that
    # was a rounding error. Sized to fill a machine it is not: without
    # this label the Job repels nothing and is repelled by nothing, so
    # it lands wherever fourteen cores happen to be free, and "happens
    # to be free" includes a node already running a story. Pod
    # anti-affinity constrains the pod being SCHEDULED against pods
    # already there; a story's own rule cannot keep a later arrival off
    # its machine. The arrival has to carry the label too.
    from kernel.drivers import k8s_placement  # noqa: PLC0415
    from kernel.drivers.k8s_spec import place_on_pool  # noqa: PLC0415

    meta_labels = {"app": "agentos-verifier", "managed-by": "kernel",
                   "agentos.ephemeral": "1",
                   "execution-id": str(spec.execution_id),
                   **k8s_placement.kind_label(k8s_placement.VERIFICATION),
                   LABEL: VIA_PREPARE, **(labels or {})}

    container = {
        "name": PREPARE,
        "image": spec.image,
        "command": spec.command,
        "env": [{"name": k, "value": v} for k, v in sorted(spec.env.items())],
        "resources": _resources(spec.cpus, spec.memory_mb),
        "volumeMounts": [{"name": "verify", "mountPath": volume.MOUNT}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            # See the docstring: the uid the criteria will execute this
            # environment as, declared once in kinds.py.
            "runAsUser": VERIFIER_UID,
        },
        "terminationMessagePolicy": "FallbackToLogsOnError",
    }

    pod_spec = {
        "restartPolicy": "Never",
        "serviceAccountName": service_account,
        "shareProcessNamespace": False,
        "securityContext": {
            "fsGroup": VERIFIER_UID,
            # ONLY WHEN IT DOES NOT ALREADY MATCH. `Always` walks the
            # whole volume on every attach, and this one carries a
            # `node_modules` -- tens of thousands of files that were
            # already given the right group by the attach that wrote
            # them.
            "fsGroupChangePolicy": "OnRootMismatch",
        },
        "volumes": [{"name": "verify",
                     "persistentVolumeClaim": {"claimName": claim}}],
        # BEFORE THE INSTALL, AND THAT ORDERING IS THE POINT. An init
        # container must exit 0 first, so a tree that could not be
        # pulled is a Job that never installs -- rather than one that
        # installs against an empty directory and reports a repository
        # with no manifests.
        "initContainers": [tree_container(kernel_image, source, token)],
        "containers": [container],
        # THE STORY'S OWN POOL, because this reruns the story's install
        # and needs the machine the story needed. Naming no pool while
        # asking for a whole tier's worth of CPU is how a Job comes to
        # sit Pending with no node able to hold it.
        "affinity": k8s_placement.one_per_node(),
    }
    place_on_pool(pod_spec, spec.node_class, namespace)

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": meta_labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            "ttlSecondsAfterFinished": TTL_AFTER_FINISHED_S,
            "template": {"metadata": {"labels": meta_labels},
                         "spec": pod_spec},
        },
    }
