"""Why a criterion's Job ended, when the answer is not an exit code.

THE INVARIANT:

    A criterion's verdict IS the exit code of its container.
    If there is no exit code, there is no verdict.

The Docker driver gets this for free. A container either exits with a
code or the API call raises, and a raise becomes `error` upstream. There
is no third thing.

Kubernetes has a third thing, and it is COMMON rather than exotic: the
run can be ended by the cluster. Spot reclaims the node, the scheduler
preempts, a taint evicts, the image will not pull, the quota refuses the
pod. In every one of those the Job reports `failed`, and the failure is
about the SUBSTRATE. Reported as a non-zero exit it reads as "the work is
wrong" — so a story gets refused because Google took a node back, and a
refusal is the one verdict that cannot be argued with (R1).

THE DEFAULT IS `error`, NOT `fail`, and that is the part worth keeping.
Every unclassifiable ending returns an infra_error rather than a
fabricated exit code, so a Kubernetes failure mode nobody anticipated —
and there will be some — costs a re-run instead of condemning a story.
The asymmetry is the same one kernel/verifier/outcome.py argues at
length: wrongly refusing is worse than wrongly not refusing.

AND EVERY SENTENCE BELOW CARRIES ITS KIND. This module is the one place
that KNOWS which of plan 5.8's rows an ending is — it holds the pod, the
condition and the reason string — so it says which in a field rather
than leaving a downstream reader to recover the fact from the prose.
kernel/drivers/infra_error.py is why prose is not a protocol.
"""

from __future__ import annotations

from kernel.drivers.infra_error import (DISRUPTED, NEVER_STARTED, OOM,
                                        UNKNOWN, UNOBSERVED, InfraError)

CRITERION = "criterion"

# THE PRIMARY SIGNAL, and the version-stable one. Kubernetes sets a
# `DisruptionTarget` condition whenever something OUTSIDE the pod ended
# it — scheduler preemption, the eviction API, taint manager, pod GC,
# and (this is the Spot case) the kubelet's graceful node shutdown. The
# reason strings below have moved between releases; the condition TYPE
# has not, so it is checked on its own and the reasons are the
# belt-and-braces.
DISRUPTION_CONDITION = "DisruptionTarget"

# A pod-level reason that means the same thing. `Terminated` is what a
# GKE Spot reclaim produces on the versions that predate the condition.
DISRUPTION_REASONS = frozenset({
    "Evicted", "Shutdown", "NodeShutdown", "Terminated",
    "TerminationByKubelet", "Preempting", "PreemptionByScheduler",
    "DeletionByTaintManager", "EvictionByEvictionAPI", "DeletionByPodGC",
    "NodeAffinity", "UnexpectedAdmissionError",
})

# The container never ran, so it checked nothing. These are `waiting`
# reasons — the container is still in front of the starting line when
# the Job is already over, which happens when the deadline expires
# during an image pull.
# THE REASONS, not the kind of the same name. `infra_error.NEVER_STARTED`
# is the row-2 label these map to; a module where one name meant both
# would leave a reader unable to tell which was intended.
NEVER_STARTED_REASONS = frozenset({
    "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "ImageInspectError", "ErrImageNeverPull", "RegistryUnavailable",
    "CreateContainerConfigError", "CreateContainerError",
    "ContainerCreating", "PodInitializing",
})

# The kubelet lost the container and cannot say how it ended. There is
# an `exitCode` field on this state and it is a placeholder, not an
# observation.
UNKNOWN_STATUS = "ContainerStatusUnknown"

# Killed for exceeding a memory ceiling THE KERNEL CHOSE. Nobody
# declared 1024MiB — kernel/verifier/kinds.py hardcodes it — so blowing
# it is the same shape as blowing the default timeout, and gets the same
# answer: it says nothing about the work. A DECLARED memory budget would
# be a `fail`, exactly as a declared timeout is, and there is no way to
# declare one yet.
OOM_KILLED = "OOMKilled"


def _get(obj, *names):
    """Walk attributes, tolerating None and absent fields.

    The SDK's models and a test's stub both answer to this, which is why
    the classifier below needs no cluster and no client library.
    """
    for name in names:
        if obj is None:
            return None
        obj = getattr(obj, name, None)
    return obj


def disrupted(pod) -> InfraError | None:
    """The sentence naming what ended this pod, or None.

    CHECKED FIRST, and a false positive here is the cheap direction: it
    produces a re-run. A false negative produces a refusal.

    THE ONLY PRODUCER OF THE `DISRUPTED` KIND, which is what makes plan
    5.8 row 3 a fact a caller can ASK for rather than a sentence it has
    to recognise.
    """
    for cond in (_get(pod, "status", "conditions") or []):
        if getattr(cond, "type", None) == DISRUPTION_CONDITION:
            reason = getattr(cond, "reason", None) or "disrupted"
            return InfraError(f"the cluster ended this run ({reason}); it "
                              f"says nothing about the work", DISRUPTED)
    reason = _get(pod, "status", "reason")
    if reason in DISRUPTION_REASONS:
        return InfraError(f"the cluster ended this run ({reason}); it says "
                          f"nothing about the work", DISRUPTED)
    return None


def _container(pod, name: str = CRITERION):
    for status in (_get(pod, "status", "container_statuses") or []):
        if getattr(status, "name", None) == name:
            return status
    return None


def classify(pod, container: str = CRITERION) -> dict:
    """`{"exit_code": int | None, "infra_error": InfraError | None}`.

    Exactly one of the two is set. `exit_code` is None whenever
    `infra_error` is — deliberately, so a caller that forgets to read
    `infra_error` gets nothing usable rather than a fabricated 1 that
    reads as a failing check. Loud beats wrong.

    `InfraError` IS a `str`, so a caller that only wants the sentence is
    unchanged; `infra_error.kind_of` is how one that has to BRANCH asks
    which of plan 5.8's rows this is.
    """
    ended = disrupted(pod)
    if ended:
        return {"exit_code": None, "infra_error": ended}

    status = _container(pod, container)
    if status is None:
        phase = _get(pod, "status", "phase") or "unknown"
        # NEVER_STARTED and not UNOBSERVED: a pod carrying no container
        # status at all was refused by quota or never scheduled, so its
        # container is in front of the starting line rather than past a
        # finish nobody watched.
        return {"exit_code": None, "infra_error": InfraError(
            f"the criterion container never reported a status "
            f"(pod phase {phase}); it did not run", NEVER_STARTED)}

    terminated = _get(status, "state", "terminated")
    if terminated is not None and getattr(terminated, "exit_code",
                                          None) is not None:
        reason = getattr(terminated, "reason", None)
        if reason == UNKNOWN_STATUS:
            # UNOBSERVED, NOT DISRUPTED. A node that stopped reporting
            # very often WAS reclaimed, but "we did not see the ending"
            # is not evidence of who ended it, and guessing row 3 here
            # would buy free re-runs for a fault that recurs.
            return {"exit_code": None, "infra_error": InfraError(
                "the node stopped reporting on this container before it "
                "finished; its exit code is a placeholder, not a result",
                UNOBSERVED)}
        if reason == OOM_KILLED:
            return {"exit_code": None, "infra_error": InfraError(
                "the criterion was killed for exceeding a memory ceiling "
                "nobody declared; nothing was checked", OOM)}
        return {"exit_code": int(terminated.exit_code), "infra_error": None}

    waiting = _get(status, "state", "waiting")
    if waiting is not None:
        reason = getattr(waiting, "reason", None) or "waiting"
        message = (getattr(waiting, "message", None) or "").strip()[:200]
        if reason in NEVER_STARTED_REASONS:
            return {"exit_code": None, "infra_error": InfraError(
                f"the criterion container never started ({reason})"
                + (f": {message}" if message else "")
                + "; nothing was checked", NEVER_STARTED)}
        return {"exit_code": None, "infra_error": InfraError(
            f"the criterion container was still waiting ({reason}) when "
            f"the Job ended; nothing was checked", NEVER_STARTED)}

    return {"exit_code": None, "infra_error": InfraError(
        "the criterion container produced no exit code, so this run "
        "reported no verdict", UNKNOWN)}
