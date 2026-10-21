"""What the VERIFIER asks of this substrate: one-shot runs.

Split from kubernetes.py, which is the execution pod's lifecycle. These
four are a different customer with different needs — a Job rather than a
pod, a different identity, and the two directions the artifact store is
used in:

    run_ephemeral   a criterion. The tree comes IN by id, because a
                    Job's pod cannot be written to before it starts.
    run_capture     the environment replay. The directory goes OUT by
                    id, because a Job's pod cannot be read after it
                    exits.

Functions taking the driver rather than methods on it, so the seam in
kubernetes.py stays a list of what the substrate does and this stays the
detail of how.
"""

from __future__ import annotations

import logging
import os

from kernel.drivers.k8s_egress import VIA_CAPTURE, VIA_STORE
from kernel.drivers.kubernetes import KubernetesUnavailable, _api
from kernel.spec import ExecutionSpec

logger = logging.getLogger(__name__)


def fetch_env(egress: str) -> dict:
    """The store's address, forwarded to the initContainer.

    `egress` is the POD'S OWN LABEL, and it is a parameter because this
    function had assumed it. See kernel/drivers/k8s_egress.py: a criterion
    Job is labelled `store` and a capture Job `proxy`, the two labels are
    permitted different ports, and pointing a pod at the port it may not
    use does not fail -- it hangs, in both directions of the artifact
    store, on the one path a criterion needs to run at all.

    READ OFF THIS PROCESS, not spelled again in a manifest. The
    conductor already resolved which store it writes to; a second
    declaration here is how a verifier ends up reading from a
    different bucket than the one the tree went into, and the
    symptom would be every criterion failing to start.
    """
    # THE RESOLVED BINDING, not the raw variable. `store.BINDING`
    # is what this process actually built its store from, defaults
    # included; forwarding the unset variable instead would let the
    # initContainer fall back to ITS default and read from a
    # different binding than the one the tree was written to.
    from kernel.artifacts.store import BINDING     # noqa: PLC0415

    env = {"ARTIFACT_STORE": BINDING}
    for key in ("ARTIFACT_BUCKET", "ARTIFACT_PREFIX"):
        value = os.environ.get(key)
        if value:
            env[key] = value

    # THE ONLY WAY OUT OF THIS POD, and which way out depends on the
    # label it carries. The namespace is default-deny; the label admits
    # the pod to exactly one port of the proxy. Without these variables
    # the client reaches for storage.googleapis.com directly, is dropped
    # by the policy, and the Job never starts — reported honestly as
    # "never started", with the cause a missing variable nobody named.
    # With the WRONG one it is worse, because a denied port hangs rather
    # than refusing.
    from kernel.drivers.k8s_egress import proxy_for   # noqa: PLC0415

    proxy = proxy_for(egress)
    if proxy and BINDING != "filesystem":
        # BOTH CASES. `fetch` and the capture sidecar are Python, which
        # reads either, but the capture directory's own tooling need not
        # be — and a half-set pair is a tool that silently bypasses the
        # proxy and hangs exactly as before. k8s_spec._proxy_env makes
        # the same argument for the sandbox.
        for name in ("HTTP_PROXY", "HTTPS_PROXY"):
            env[name] = proxy
            env[name.lower()] = proxy
        # The cluster's own names are not the proxy's business.
        # THE SHARED LIST, which names the metadata server. Without it
        # `storage.Client()` in the capture sidecar cannot determine its
        # project and the upload dies -- exactly what broke every
        # environment replay. See k8s_egress.NO_PROXY.
        from kernel.drivers.k8s_egress import NO_PROXY  # noqa: PLC0415

        env["NO_PROXY"] = NO_PROXY
        env["no_proxy"] = NO_PROXY
    return env

def fetch_image() -> str:
    """OUR image, not the contract's.

    The initContainer runs `python -m kernel.artifacts.fetch`, which
    exists in the execution plane's image and in no base image a
    contract will ever name. Falling back to `spec.image` would give
    a pod that starts, fails on `No module named kernel`, and leaves
    the criterion in PodInitializing — reported honestly as "never
    started", but with the cause a deployment gap nobody named.
    """
    image = os.environ.get("AGENTOS_EXECUTION_IMAGE")
    if not image:
        raise KubernetesUnavailable(
            "AGENTOS_EXECUTION_IMAGE is not set, so there is no image "
            "that can fetch the committed tree into a criterion's pod. "
            "The conductor's own image is the one to name — see "
            "deploy/k8s/conductor.yaml.")
    return image

async def run_ephemeral(driver, spec: ExecutionSpec,
                        timeout: float = 300.0,
                        upload=None, also_upload=None,
                        services: list | None = None,
                        fetch: list | None = None) -> dict:
    """One criterion, as a Job. Returns exit_code/output/timed_out.

    `timeout` is the DECLARED budget and reaches the Job as
    activeDeadlineSeconds — see k8s_job.py. The 300-second default in
    this signature is the Docker driver's and is a floor for callers
    that never declared one, not a planning assumption: a real
    suite runs for tens of minutes and says so through
    `contract.verify_timeout_s`.

    `fetch` is `[(destination_dir, artifact_id), ...]` — the same
    shape as `upload`, with ids where that has bytes. It becomes an
    initContainer that unpacks each object into an emptyDir the
    criterion mounts. This is how the tree arrives on a substrate
    where nothing can be written into a pod before it runs, and it
    is why TREE_MODEL is "fetch" here and "push" on Docker.

    `upload` is REFUSED rather than ignored. Silently dropping the
    tree would run every criterion against an empty container and
    report the results as if they meant something.
    """
    if upload or also_upload:
        raise NotImplementedError(
            "run_ephemeral(upload=...) has no Kubernetes equivalent: "
            "there is no pre-start push into a Job's pod. Put the tree "
            "in the artifact store and pass `fetch=[(dir, id)]` "
            "instead — the caller picks between them on the driver's "
            "TREE_MODEL. Refusing rather than running the criterion "
            "against an empty container.")

    from kernel.drivers.k8s_job import job_manifest
    from kernel.drivers.k8s_run import run_job

    client, _ = _api()
    core = driver._core_api()
    batch = client.BatchV1Api()
    name = f"crit-{os.urandom(6).hex()}"
    manifest = job_manifest(spec, name, namespace=driver.namespace,
                            timeout_s=timeout,
                            # NOT self.service_account: see
                            # __init__. The executor must not gain
                            # bucket read by sharing an identity
                            # with the thing that checks it.
                            service_account=driver.verifier_service_account,
                            sidecars=services,
                            fetch=fetch,
                            fetch_image=fetch_image() if fetch else "",
                            # `job_manifest` labels this pod `store`.
                            fetch_env=(fetch_env(VIA_STORE)
                                       if fetch else None))
    return await run_job(batch, core, manifest,
                         namespace=driver.namespace, timeout_s=timeout)

async def run_capture(driver, spec: ExecutionSpec, capture: str,
                                timeout: float = 300.0,
                                upload=None, fetch=None) -> dict:
    """Run a container and take a directory away from it.

    THE DIRECTORY LEAVES UNDER ITS OWN POWER. The Docker driver
    pulls it back with `GET /containers/{id}/archive` before the
    container is destroyed; a Job's pod cannot be read after it
    exits, so instead a sidecar in the same pod waits for the work
    container to finish, tars the directory into the artifact store
    and prints the id. See kernel/drivers/k8s_capture.py.

    Returns run_ephemeral's dict plus `archive`: the captured
    directory as a tar rooted at its own basename, which is the
    shape the Docker driver returns and the shape `only_new` and
    `rebase_deps_tar` read.

    A CAPTURE THAT DID NOT HAPPEN IS AN INFRA ERROR, not an empty
    tar. The callers turn a failed replay into `error` for every
    criterion -- a check that could not be given its dependencies
    did not run -- and handing back an empty archive with exit 0
    would instead have them run against an environment with nothing
    in it and report the results as if they meant something.
    """
    if upload:
        raise NotImplementedError(
            "run_ephemeral_capture(upload=...) has no Kubernetes "
            "equivalent: there is no pre-start push into a Job's pod. "
            "Pass `fetch=[(dir, id)]` instead -- the caller picks "
            "between them on the driver's TREE_MODEL.")

    from kernel.drivers.k8s_capture import (CAPTURE, WORK,
                                            artifact_id_from,
                                            capture_manifest)
    from kernel.drivers.k8s_run import run_job

    client, _ = _api()
    core = driver._core_api()
    batch = client.BatchV1Api()
    # `capture_manifest` labels this pod `capture`, which carries the
    # FULL port — what its work container needs to run the tree's own
    # setup script, and which still reaches the store (squid's
    # `full_hosts` includes .googleapis.com) — PLUS the metadata server,
    # which `proxy` deliberately withholds and this pod's sidecar cannot
    # do without. The label and the address are one decision, so this
    # call and `capture_manifest` must name the same class.
    image, env = fetch_image(), fetch_env(VIA_CAPTURE)
    name = f"cap-{os.urandom(6).hex()}"
    manifest = capture_manifest(
        spec, name, namespace=driver.namespace, timeout_s=timeout,
        service_account=driver.capture_service_account,
        capture_dir=capture, capture_image=image, capture_env=env,
        fetch=fetch, fetch_image=image, fetch_env=env)

    result = await run_job(batch, core, manifest,
                           namespace=driver.namespace, timeout_s=timeout,
                           container=WORK, extra_logs=(CAPTURE,))

    artifact_id = artifact_id_from(
        (result.get("extra") or {}).get(CAPTURE, ""))
    if artifact_id is None:
        # AND WHAT THE SIDECAR ACTUALLY SAID. Without this the failure
        # reads "stored nothing" and stops there -- which is the
        # conclusion, not the cause, and it is the only thing that
        # reaches the criterion's detail. Diagnosing it on `next` meant
        # reading manifests and guessing at shared volumes, three rounds
        # of it, when the answer was in a log this function already held
        # in `result["extra"]` and discarded.
        #
        # The whole point of `extra_logs=(CAPTURE,)` is that this
        # container's output is the return channel; a marker it did not
        # print is exactly when its other output matters most.
        said = ((result.get("extra") or {}).get(CAPTURE) or "").strip()
        tail = said[-800:] if said else "(the sidecar logged nothing at all)"
        return {**result, "archive": b"", "infra_error": (
            result.get("infra_error")
            or "the capture sidecar stored nothing, so the environment "
               f"replay has no directory to carry. It said:\n{tail}")}

    from kernel.artifacts.aio import aget            # noqa: PLC0415

    return {**result, "archive": await aget(artifact_id)}
