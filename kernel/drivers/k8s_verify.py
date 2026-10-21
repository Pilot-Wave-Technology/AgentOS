"""The verification pod's lifecycle: make it, wait for it, address it, end it.

Split from `kubernetes.py` for the reason `k8s_ephemeral` was: that
file is the EXECUTION pod's lifecycle, and this is a different customer
with different rules. The two differ on every question that matters --

    execution pod          verification pod
    ──────────────────     ─────────────────────────────────────────
    one claim, RWOP        a SECOND claim, also RWOP, different name
    `Never`, one shot      `Always`, warm across generations
    the executor's uid     the Verifier's, and the tier's memory
    egress via squid       none at all

-- so sharing `create`/`destroy` would mean four branches inside
methods whose docstrings currently state one story each.

Functions taking the driver rather than methods on it, so the seam in
`kubernetes.py` stays a list of what the substrate does for an
execution and this stays what it does for a verification.

IDEMPOTENT ON PURPOSE, BOTH HALVES. Plan 5.8 row 3: a preempted
verification pod must NOT become a verdict -- it says nothing about the
work -- so the answer is to build it again on the surviving claim and
re-run. That recovery is `ensure` called a second time, so "already
exists" is the ordinary case on the path that matters most rather than
an error, exactly as `_ensure_pvc` argues for the workspace.
"""

from __future__ import annotations

import asyncio
import logging
import os

from kernel.drivers import k8s_bridge
from kernel.drivers.k8s_call import call
from kernel.drivers.kubernetes import (_already_exists, _exit_detail,
                                       _init_detail, _not_found)
from kernel.sizes import Size
from kernel.verifier import pod as verify_pod
from kernel.verifier.prepare_job import FETCH

logger = logging.getLogger(__name__)

# Longer than the execution pod's two minutes. That one waits on a
# clone; this one waits on a PVC ATTACH, and plan 5.5 measures the
# detach/attach handover from the prepare Job at ~30s on a GCE disk --
# on top of pulling a contract image nobody has pinned to this node
# yet. A ceiling too tight reads as "the pod never became ready" when
# what happened is that a disk was still moving.
READY_TIMEOUT_S = 300.0


async def ensure(driver, execution_id, *, image: str, size: Size,
                 volume_class: str = "standard",
                 labels: dict | None = None) -> str:
    """The claim then the pod. Returns the pod name.

    THE CLAIM FIRST AND THE CLAIM SURVIVES. `destroy` below takes the
    pod alone, so a pod replaced after preemption re-attaches the
    environment the prepare Job already built rather than paying for it
    again -- which is the same argument `kubernetes.destroy` makes for
    the workspace, for the same reason.
    """
    api = driver._core_api()
    await _ensure(api.create_namespaced_persistent_volume_claim,
                  driver.namespace,
                  verify_pod.pvc_manifest(execution_id,
                                          namespace=driver.namespace,
                                          size=size,
                                          volume_class=volume_class),
                  verify_pod.claim_name(execution_id))
    name = verify_pod.pod_name(execution_id)
    await _ensure(api.create_namespaced_pod, driver.namespace,
                  verify_pod.pod_manifest(
                      execution_id, namespace=driver.namespace,
                      image=image,
                      # THE KERNEL'S IMAGE, and only to deliver the
                      # shim. Resolved HERE and not inside the manifest
                      # builder for the reason `k8s_bridge.kernel_image`
                      # gives: which image is deployed is a deployment
                      # fact, and reaching into the environment from a
                      # pure function is what made seven unrelated tests
                      # depend on a variable.
                      bridge_image=k8s_bridge.kernel_image(),
                      service_account=driver.verifier_service_account,
                      size=size, labels=labels),
                  name)
    logger.info("verification pod %s created (%s cpu, %sMi, image=%s)",
                name, size.cpus, size.memory_mb, image,
                extra={"event": "verify.pod"})
    return name


async def _ensure(create, namespace: str, body: dict, name: str) -> None:
    try:
        await call(create, namespace=namespace, body=body)
    except Exception as exc:                                # noqa: BLE001
        if not _already_exists(exc):
            raise
        logger.info("verification %s already exists; adopting it", name)


async def await_ready(driver, name: str,
                      timeout: float = READY_TIMEOUT_S) -> bool:
    """The shim answers /health, or raise.

    READINESS AND NOT PHASE, and here the difference is real rather
    than theoretical: this pod's container is ready within a second of
    starting and its tree arrives later, so `Running` would be true
    long before anything could be asked of it. The manifest carries a
    `readinessProbe` on /health precisely so this condition means what
    `kubernetes.await_ready`'s docstring has always claimed it does.
    """
    api = driver._core_api()
    deadline = asyncio.get_event_loop().time() + timeout
    pod, last = None, "unknown"
    while asyncio.get_event_loop().time() < deadline:
        pod = await call(api.read_namespaced_pod, name=name,
                         namespace=driver.namespace)
        last = pod.status.phase
        if last == "Failed":
            # WITH THE REASON. A pod that will not start is usually a
            # claim still attached to the prepare Job (plan 5.5's RWOP
            # handover) or an image the contract named and the registry
            # does not have, and both are invisible once the object is
            # deleted.
            raise RuntimeError(
                f"verification pod {name} reached Failed before becoming "
                f"ready{_exit_detail(pod)}")
        if any(c.ready for c in (pod.status.container_statuses or [])):
            return True
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"verification pod {name} not ready after {timeout}s "
        f"(phase={last}{_init_detail(pod) if pod else ''})")


async def address(driver, name: str) -> str:
    """The pod's own IP, because there is no loopback route to it.

    Read per call rather than cached at create, the same as
    `resolve_bridge_host`: with two verifier replicas the one that
    created the pod is not necessarily the one adjudicating the next
    round, and a cache miss that fell back to `localhost` would post
    tool calls at the verifier's own process.
    """
    api = driver._core_api()
    pod = await call(api.read_namespaced_pod, name=name,
                     namespace=driver.namespace)
    ip = getattr(getattr(pod, "status", None), "pod_ip", None)
    if not ip:
        raise RuntimeError(
            f"verification pod {name} has no address yet, so its bridge "
            f"cannot be reached and no criterion can be run in it.")
    return ip


async def destroy(driver, name: str, grace: int = 15) -> None:
    """Delete the POD. The claim is the execution's, not the pod's.

    ALREADY GONE IS SUCCESS -- the rule `kubernetes.destroy` applies to
    a missing pod, and it matters more here: the caller reaching this is
    often a reaper walking a list, and raising on one collected object
    stops it reaching the next.
    """
    api = driver._core_api()
    try:
        await call(api.delete_namespaced_pod, name=name,
                   namespace=driver.namespace, grace_period_seconds=grace)
    except Exception as exc:                                 # noqa: BLE001
        if not _not_found(exc):
            raise


async def release(driver, execution_id) -> bool:
    """Delete the verification claim. The other half of `destroy`.

    TWO TRIGGERS AND NOT THREE. Plan 5.7 reaps the POD on terminal, on a
    manifest change and on idle; the CLAIM goes on the first two only.
    An idle pod's environment is still the one the next round wants, and
    deleting it there would rebuild a `.venv` because nobody was looking
    -- which is the cost this whole design exists to remove.
    """
    name = verify_pod.claim_name(execution_id)
    api = driver._core_api()
    try:
        await call(api.delete_namespaced_persistent_volume_claim,
                   name=name, namespace=driver.namespace)
    except Exception as exc:                                 # noqa: BLE001
        if _not_found(exc):
            return False
        raise
    logger.info("verification claim %s released", name,
                extra={"event": "reconcile"})
    return True


async def claim_exists(driver, claim: str) -> bool:
    """Does that claim exist? The read half of `release`.

    HERE FOR THE REASON `run_job_on_claim` IS HERE: this file is the
    verification claim's lifecycle, and `kubernetes.py` is the EXECUTION
    pod's. It was `kernel/verifier/handover.has_claim` until 5.17 item
    11, reaching through `driver._core_api()` and importing
    `kubernetes._not_found` from inside the verifier.

    404 IS AN ANSWER, NOT A FAULT -- the same rule `release` applies to
    the same object, and the one that makes the whole rebuild gate inert
    for a run that was never given a volume. Anything else RAISES: a
    403, a timeout or an unreachable API server mean we do not know, and
    reporting "no claim" for "could not ask" would send a prepare Job at
    a disk that is actually attached.
    """
    api = driver._core_api()
    try:
        await call(api.read_namespaced_persistent_volume_claim,
                   name=claim, namespace=driver.namespace)
    except Exception as exc:                                 # noqa: BLE001
        if _not_found(exc):
            return False
        raise
    return True


async def run_job_on_claim(driver, spec, *, claim: str, timeout_s: float,
                           tree_source: str, tree_token: str) -> dict:
    """E17 — a Job against the verification claim, through the seam.

    HOISTED OUT OF `prepare_volume._run`, which imported `k8s_run` and
    `kubernetes._api` from inside the verifier and reached past the
    driver to do it. That worked, and it put a substrate's private
    module import in a function every substrate calls: a driver with no
    claims got a traceback out of the middle of a verification instead
    of the sentence `build_deps` has always given for the same class of
    fact. `Driver.CLAIM_JOB_MODEL` is the question and this is the
    answer for the one substrate that says yes.

    HERE AND NOT IN `kubernetes.py` for the reason this whole module
    exists: that file is the EXECUTION pod's lifecycle, and the
    verification claim is a different customer -- a second RWOP claim,
    a different identity, a different reaper.

    `_api()` STILL, AND ONLY FOR THE BATCH CLIENT. `driver._core_api()`
    memoises a CoreV1Api and loads the kubeconfig; there is no batch
    equivalent on the driver, and adding a second memoised client to it
    to save one line would give `aclose` a second thing to forget.
    """
    from kernel.drivers.k8s_run import run_job               # noqa: PLC0415
    from kernel.drivers.kubernetes import _api               # noqa: PLC0415
    from kernel.verifier.prepare_job import (PREPARE,        # noqa: PLC0415
                                             prepare_manifest)

    client, _ = _api()
    manifest = prepare_manifest(
        spec, f"prep-{os.urandom(6).hex()}", namespace=driver.namespace,
        claim=claim,
        # NOT the executor's identity, for the reason the driver's own
        # comment gives: "execution holds nothing" is the property the
        # pod design rests on. This Job needs no cloud identity at all --
        # it reaches package indexes through the proxy, pulls the tree
        # from another pod, and writes to a disk -- so the verifier's is
        # the narrowest one already here.
        service_account=driver.verifier_service_account,
        # THE KERNEL'S IMAGE, and only to deliver the fetch. Resolved
        # HERE and not inside the manifest builder, for the reason
        # `k8s_bridge.kernel_image` gives: which image is deployed is a
        # deployment fact, and reaching into the environment from a pure
        # function is what made seven unrelated tests depend on a
        # variable.
        kernel_image=k8s_bridge.kernel_image(),
        source=tree_source, token=tree_token,
        timeout_s=timeout_s)
    logger.info("prepare Job %s starting on %s (execution %s)",
                manifest["metadata"]["name"], claim, spec.execution_id,
                extra={"event": "verify.prepare"})
    result = await run_job(client.BatchV1Api(), driver._core_api(), manifest,
                           namespace=driver.namespace, timeout_s=timeout_s,
                           container=PREPARE, extra_logs=(FETCH,))
    # THE FETCH'S LOG, ALWAYS AND NOT ONLY ON FAILURE. Plan 13: a
    # container that says nothing leaves a silence identical to one that
    # never ran, and the init container is where "the tree never
    # arrived" is distinguishable from "the install failed". `run_job`
    # deletes the Job in a `finally`, so this is the last moment either
    # log can be read at all.
    logger.info("prepare Job %s finished: exit=%s infra=%s\n%s",
                manifest["metadata"]["name"], result.get("exit_code"),
                result.get("infra_error"),
                (result.get("extra") or {}).get(FETCH, ""),
                extra={"event": "verify.prepare"})
    return result
