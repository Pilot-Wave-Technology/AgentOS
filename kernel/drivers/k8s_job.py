"""A criterion is a Job. Manifest only — no SDK, no cluster.

The verifier's whole mechanism is a throwaway container created from a
pinned digest that runs one command and dies. On Kubernetes that is a
Job, and three of its fields carry decisions rather than defaults.

E2 — A JOB MAY NOW CARRY SEVERAL CRITERIA, and this manifest does not
know which. `spec.command` is `sh -c <one check>` or `sh -c <the batch
runner>`; from here they are the same pod with the same identity, the
same uid, the same `egress: store` and the same absence of a route to
the forge. Two things it changes for a reader of this file:

  the exit code is not always the verdict   for a batched Job it means
                                            only "the runner ran", and
                                            the verdicts are JSON lines
                                            in the pod's log. See
                                            kernel/verifier/runner.py.
  the deadline may be a SUM                 of the batch's per-check
                                            budgets, plus headroom. The
                                            paragraph below still holds
                                            unchanged — it is still the
                                            CALLER's declared number and
                                            never a constant — and
                                            kernel/verifier/batch.py
                                            splits a batch so the sum
                                            never reaches
                                            MAX_DEADLINE_S, because a
                                            clamped batch dies mid-run
                                            and loses results that were
                                            already earned.

HOW LONG A CRITERION MAY RUN IS DECLARED, NOT ASSUMED.

The planning note said "~3 per story, 5 minutes". Five minutes is wrong
for the case that matters: a large codebase's suite runs for tens of
minutes, `contract.verify_timeout_s` (or a criterion's `spec.timeout_s`)
declares it, and kernel/verifier/budget.py caps it at two hours.

So `activeDeadlineSeconds` comes from the caller's timeout and nothing
else. A Job killed at an arbitrary constant reports a timeout, and
`from_exit` classifies an undeclared timeout as `error` — which tells
the executor its environment is broken. It then edits a manifest and a
setup script it has no problem with, re-proposes, times out again, and
fails with "N legs without converging" and no recorded reason. That loop
is the reason budget.py exists, and a hardcoded deadline here would
reintroduce it one layer down.

backoffLimit: 0, AND THAT IS THE VERDICT'S INTEGRITY.

A Job that retried on its own would run a flaky test twice and report
the second answer. `flake` and `fail` are different values in a
four-valued verdict precisely so an intermittent test does not refuse a
story, and a silent retry collapses them. Retrying is the adjudicator's
decision, made with both observations in front of it.

It matters MORE once a Job is a batch: a retry would re-run every check
in it, including the ones that already passed, and the second pod's
lines would arrive looking exactly like the first's.

SPOT IS STILL RIGHT HERE, for a different reason than the executor. A
verifier is idempotent and stateless: it rebuilds a committed tree in a
fresh container, so a preemption costs a re-run rather than lost work.
The executor's case rests on the PVC surviving; this one needs nothing
to survive at all.
"""

from __future__ import annotations

from kernel.drivers.k8s_spec import spec_uid
from kernel.spec import ExecutionSpec

# A criterion holds a slot and a pod for its whole run, so an unbounded
# deadline is a way to hold both for ever by writing a number in a file.
# The same ceiling budget.py applies, restated here because a manifest
# built from an unclamped caller would carry it into the cluster.
MAX_DEADLINE_S = 7200

# Beyond the deadline, how long Kubernetes may take to remove the Job.
# Short: a finished criterion's pod is evidence nobody reads, and the
# results are already in Postgres.
TTL_AFTER_FINISHED_S = 300

# `nobody`. USED ONLY WHEN THE SPEC DECLARES NOTHING — the verifier's own
# uid lives in kernel/verifier/kinds.py and arrives on `spec.user`, and
# spelling 65534 here as well would be two declarations of one fact, the
# dominant bug class in this repository. This is the floor for a caller
# that declared no user, not the verifier's identity.
FALLBACK_UID = 65534

# What the initContainer runs. See kernel/artifacts/fetch.py.
FETCH_ENTRYPOINT = ["python", "-m", "kernel.artifacts.fetch"]


def _uid(spec) -> int:
    """The verifier's uid, from the spec that declared it."""
    declared = spec_uid(spec)
    return FALLBACK_UID if declared is None else declared


def fetch_volumes(fetch: list) -> tuple[list, list]:
    """An emptyDir per distinct destination, and the mounts onto it.

    The initContainer writes and the criterion reads, so the volume is
    what makes the tree cross between them. emptyDir rather than a
    claim: it lives exactly as long as the pod, which is exactly as long
    as anything needs a criterion's copy of the tree.
    """
    dests = sorted({dest for dest, _ in fetch})
    names = [f"fetched-{i}" for i, _ in enumerate(dests)]
    volumes = [{"name": n, "emptyDir": {}} for n in names]
    mounts = [{"name": n, "mountPath": d} for n, d in zip(names, dests)]
    return volumes, mounts


def fetch_init(spec, fetch: list, *, image: str, env: dict,
               mounts: list) -> dict:
    """The container that pulls the tree in before the criterion runs.

    THE SAME UID AS THE CRITERION, and that is load-bearing rather than
    tidy: it writes the files the criterion then has to read and
    execute. A root initContainer would leave a tree the criterion
    cannot enter, and the failure would look like a broken check.
    """
    args = [part for dest, artifact_id in fetch for part in (dest, artifact_id)]
    return {
        "name": "fetch-tree",
        "image": image,
        "command": [*FETCH_ENTRYPOINT, *args],
        # THE STORE'S ADDRESS, AND NOTHING ELSE. Not the criterion's
        # environment and not the contract's declared secrets: this
        # container's whole job is to turn an id into bytes.
        "env": [{"name": k, "value": v} for k, v in sorted(env.items())],
        "volumeMounts": mounts,
        "resources": {
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": {"cpu": "500m", "memory": "1Gi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": _uid(spec),
        },
    }


def job_manifest(spec: ExecutionSpec, name: str, *, namespace: str,
                 timeout_s: float, service_account: str,
                 labels: dict | None = None,
                 sidecars: list | None = None,
                 fetch: list | None = None,
                 fetch_image: str = "", fetch_env: dict | None = None) -> dict:
    """One criterion — or one batch of them — as a Job.

    `sidecars` are declared services — a database a criterion needs.
    THEY GO IN THIS POD, which is what makes the private network the
    Docker driver builds with `create_network` unnecessary: a pod's
    network namespace already is a private network, reachable on
    localhost by this criterion and by nothing else. It costs nothing
    and cannot be misconfigured to admit a second criterion.

    AND THAT LAST CLAUSE IS WHY A CONTRACT WITH SERVICES IS NOT
    BATCHED. Sidecars are a pod-level fact: batching would put two
    criteria on one database and let the first leave a row the second
    reads, which services.py is explicit is worse than a slow
    verification because it passes for a reason nobody can see.

    `fetch` is `[(destination_dir, artifact_id), ...]` — the same shape
    as the Docker driver's `upload=(dir, tar)`, with ids where that has
    bytes. It becomes an initContainer that unpacks each object into an
    emptyDir the criterion mounts, which is how the committed tree
    reaches a pod that cannot be written to before it starts.
    """
    deadline = max(1, min(int(timeout_s), MAX_DEADLINE_S))
    # `store`, NOT `proxy`, and the difference is F2's defence.
    #
    # An execution pod needs a package index, a git host and a model
    # API. A criterion needs the artifact store its committed tree
    # arrives from, and on Docker it gets `network_mode: none` and needs
    # nothing at all — dependencies reach it through the recorded
    # environment replay, which exists so a check never has to fetch.
    #
    # This label admits it to one port of the egress proxy whose
    # allowlist is one entry long. Giving it `proxy` would hand every
    # criterion the executor's reach, and a criterion that can reach the
    # internet can fetch a version of itself that passes.
    # THE LABEL AND THE PROXY ARE ONE DECISION, so both come from
    # k8s_egress -- `fetch_env(VIA_STORE)` in k8s_ephemeral is the other
    # half. `store` is the NARROWER class and is the point of the
    # paragraph above: a criterion must not reach the forge.
    from kernel.drivers.k8s_egress import LABEL, VIA_STORE  # noqa: PLC0415

    # `agentos.io/kind: verification` for the same reason the execution
    # pod carries `execution`: a criterion re-running a story's test
    # suite is exactly as hungry as the story was, and sharing a machine
    # with one costs both of them without failing either.
    from kernel.drivers import k8s_placement  # noqa: PLC0415
    from kernel.drivers.k8s_spec import place_on_pool  # noqa: PLC0415

    meta_labels = {"app": "agentos-verifier", "managed-by": "kernel",
                   "agentos.ephemeral": "1",
                   **k8s_placement.kind_label(k8s_placement.VERIFICATION),
                   LABEL: VIA_STORE, **(labels or {})}
    if spec.execution_id is not None:
        meta_labels["execution-id"] = str(spec.execution_id)

    containers = [{
        "name": "criterion",
        "image": spec.image,
        "command": spec.command,
        "env": [{"name": k, "value": v} for k, v in sorted(spec.env.items())],
        "resources": {
            "requests": {"cpu": str(spec.cpus),
                         "memory": f"{spec.memory_mb}Mi"},
            "limits": {"cpu": str(spec.cpus),
                       "memory": f"{spec.memory_mb}Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            # A DIFFERENT uid from the executor's, which is one of the
            # four properties that make a verifier's container
            # trustworthy: nothing the executor left owned and writable
            # on the path can be executed by the thing checking it.
            # FROM THE SPEC, which is where the caller declared it.
            "runAsUser": _uid(spec),
        },
    }]

    pod_spec: dict = {
        "restartPolicy": "Never",
        "serviceAccountName": service_account,
        "shareProcessNamespace": False,
        # THE SAME POOL THE STORY USED, because a criterion re-runs the
        # story's own work and needs the machine the story needed. A
        # verification given a small node for a large story does not
        # report that it was under-resourced; it reports that the tests
        # timed out, which reads as a finding about the work.
        # AND THE STORY'S OWN ENVIRONMENT, for the same reason it
        # gets the story's own class: a verification that ran on the
        # other environment's machines reports nothing about that,
        # it just spends the other environment's money.
        # ITS OWN MACHINE, which is the part of "the verifier gets its
        # own node" that is enforced rather than merely intended. The
        # rule repels executions AND other verifications, so two
        # criteria from one contract do not land together either.
        "affinity": k8s_placement.one_per_node(),
    }
    # AFTER THE LITERAL, and as one call, because the selector and the
    # toleration are two halves of one decision. Applied separately,
    # the way this breaks is a pod that may use the pool and is
    # repelled by it -- Pending, reporting nothing.
    place_on_pool(pod_spec, spec.node_class, namespace)
    if fetch:
        volumes, mounts = fetch_volumes(fetch)
        containers[0]["volumeMounts"] = mounts
        pod_spec["volumes"] = volumes
        pod_spec["initContainers"] = [
            fetch_init(spec, fetch, image=fetch_image or spec.image,
                       env=fetch_env or {}, mounts=mounts)]

    containers.extend(sidecars or [])
    pod_spec["containers"] = containers

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": meta_labels},
        "spec": {
            # NO RETRIES. See the module docstring: a silent retry
            # collapses `flake` into `pass`.
            "backoffLimit": 0,
            # THE DECLARED TIMEOUT, not a constant.
            "activeDeadlineSeconds": deadline,
            "ttlSecondsAfterFinished": TTL_AFTER_FINISHED_S,
            "template": {
                "metadata": {"labels": meta_labels},
                "spec": pod_spec,
            },
        },
    }


def service_sidecar(name: str, image: str, env: dict | None = None,
                    cpus: float = 0.5, memory_mb: int = 512) -> dict:
    """A declared service, in the criterion's own pod.

    The Docker driver gives each criterion a private network and attaches
    the service to it. A pod does not need one: its namespace is already
    private, the service answers on localhost, and there is no network
    object to leak, name-collide, or forget to remove.
    """
    return {
        "name": name,
        "image": image,
        "env": [{"name": k, "value": v} for k, v in sorted((env or {}).items())],
        "resources": {
            "requests": {"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
            "limits": {"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
        },
    }
