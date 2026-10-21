"""The Kubernetes substrate driver.

ONE POD PER EXECUTION, worker and sandbox together — see
docs/refactor/decisions/p1-one-pod-per-execution.md and
kernel/drivers/k8s_spec.py, which builds the manifest. This module is the
API calls; every decision about the pod's shape lives there, and is unit
tested without a cluster.

WHAT `container_id` MEANS HERE. A pod name. The kernel treats the id as
an opaque string it got from `create` and hands back to `destroy`,
`await_ready` and the rest, so a pod name satisfies the contract exactly
as a container id does. Nothing above the driver parses it.

THE PVC OUTLIVES THE POD, and that is the whole reason Spot is
defensible: a preempted pod's replacement re-attaches the same workspace
and resumes from the last commit. So `destroy` deletes the POD and not
the claim. The claim belongs to the EXECUTION and is reaped when the
execution reaches a terminal state — which is the reconciler's job, and
is why `reconcile_all` had to exist before this did.

THE SDK IS IMPORTED LAZILY, for the reason kernel/artifacts/gcs.py gives
at more length: E0's image does not carry it, and E5's module-load check
imports every module under kernel/. A module-level import would fail
that gate on a machine behaving exactly as designed.
"""

from __future__ import annotations

import asyncio
import logging
import os

from kernel.driver import Driver
from kernel.drivers import k8s_bridge, k8s_sqlproxy
from kernel.drivers.k8s_spec import (SANDBOX, WORKER, pod_manifest,
                                     pvc_manifest)
from kernel.drivers.k8s_call import call
from kernel.ownership import (CLAIM_PREFIX, KERNEL_SELECTOR,  # noqa: F401
                              claim_name, workspace_execution)
from kernel.spec import ExecutionSpec, require_supported, validate_spec

logger = logging.getLogger(__name__)

# ONE SANDBOX PER POD, so the port is fixed. Docker needed a 9200-9399
# allocator because many sandboxes shared the worker's namespace; here
# the namespace holds exactly one, and a 200-port allocator that
# allocates one is dead code pretending to be a feature.
BRIDGE_PORT = 9100

# WHAT THIS DRIVER HANDS THE REAPERS, and therefore what they can reach
# at all. Both listers below are the narrow end of the funnel: a reaper
# cannot destroy what it was never shown, so the selector and the claim
# name parse are ownership decisions rather than query details. They
# come from `kernel/ownership.py` with every other reaper's, because the
# incident on execution 208 was two spellings of one rule disagreeing.
#
# The name re-exports are kept: dispatch and `release_workspace` in this
# file build claim names, and callers reach for them here.
LABEL_SELECTOR = KERNEL_SELECTOR


class KubernetesUnavailable(Exception):
    """The driver was selected and its client is not installed."""


def _api():
    """The client, or a refusal naming what is missing and why."""
    try:
        from kubernetes import client, config            # noqa: PLC0415
    except ImportError as exc:                           # pragma: no cover
        raise KubernetesUnavailable(
            "AGENTOS_DRIVER=kubernetes but the `kubernetes` package is not "
            "installed. Install it, or set AGENTOS_DRIVER=docker — E0 runs "
            "Docker on purpose and does not carry the client."
        ) from exc
    return client, config


class KubernetesDriver(Driver):
    """Executions as pods, one apiece."""

    # WHAT THIS SUBSTRATE CAN PROVIDE. Unlike Docker's, these are real
    # choices: GKE Sandbox has run on Autopilot since 1.27.4-gke.800 as a
    # per-pod runtimeClassName, at no extra charge.
    #
    # `vm` is absent deliberately. A hardware-virtualised boundary is a
    # different node pool and a different cost model, and listing it here
    # before one exists would mean a spec asking for it is accepted and
    # silently given a syscall sandbox — the downgrade this contract
    # exists to prevent.
    # A pod's namespace is already private, so a declared service is a
    # container in the criterion's own Job rather than a peer on a
    # network that had to be created, named, and removed.
    SERVICE_MODEL = "sidecar"

    # A Job's pod cannot be read after it exits, so the directory
    # LEAVES under its own power: a sidecar tars it into the artifact
    # store and the caller reads it back by id. Different mechanism
    # from Docker's `GET /archive`, same capability, which is what this
    # field names — see kernel/drivers/k8s_capture.py.
    CAPTURE_MODEL = "artifact"

    # A Job's pod cannot be WRITTEN to before it starts either, so the
    # tree is fetched by id rather than pushed as bytes. See
    # kernel/artifacts/fetch.py.
    TREE_MODEL = "fetch"
    # The claim is ReadWriteOncePod and belongs to the execution's pod.
    # See KubernetesDriver.sandbox_attachments for the same boundary
    # stated about the sandbox: this driver does not have the Docker
    # process model, and pretending otherwise fails later and elsewhere.
    SANDBOX_MODEL = "pod"
    WORKSPACE_MODEL = "pod"

    # A batch/v1 Job mounting the verification PVC. Plan 5's prepare
    # step: the install writes `volume.ENV` directly and the only thing
    # that leaves the Job is an exit code.
    CLAIM_JOB_MODEL = "job"
    # And so the tree a criterion is judged against is taken where the
    # tree is. The conductor cannot open the claim to run `git archive`
    # against it, and running one anyway produces a tar of nothing --
    # which the verifier cannot tell from a story that committed
    # nothing. kernel/workspace/podfs.py serves it from the worker.
    ARCHIVE_MODEL = "pod"

    CAPABILITIES = {
        "isolation": ("shared", "syscall"),
        "volume_class": ("standard", "fast"),
        # One word per pool in scripts/infra/gke_standard_pools.sh, and
        # the same three the size ladder uses. "gpu" is absent because no
        # pool wears the label; a spec asking for it is refused by name
        # rather than scheduled onto a machine that cannot serve it.
        "node_class": ("small", "medium", "large"),
    }

    def __init__(self, namespace: str | None = None,
                 service_account: str | None = None) -> None:
        # B1: no default inside environ.get — the fallback is spelled out
        # so the value the process runs with is visible in the code.
        #
        # THE FALLBACK IS NOT AN ENVIRONMENT NAME. It used to be
        # `agentos-staging`, which is one of OUR namespaces: any
        # installation that did not set AGENTOS_K8S_NAMESPACE aimed
        # its pods at a namespace named after our production
        # environment, and on our own clusters it would have aimed
        # them at the real one. `agentos` is the plain
        # single-environment default; every deployment with more
        # than one namespace sets the variable.
        self.namespace = namespace or os.environ.get("AGENTOS_K8S_NAMESPACE") \
            or "agentos"
        self.service_account = (
            service_account or os.environ.get("AGENTOS_K8S_SERVICE_ACCOUNT")
            or "agentos-execution")
        # NAMED, NEVER READ. The composing init container clones private
        # repositories and needs the broker bearer; this driver never
        # holds the value, it puts the secret's NAME in the manifest and
        # Kubernetes delivers it to that one container. Routing it
        # through the spec instead would have to defeat
        # `assert_no_credentials`, which refuses any spec env key ending
        # in _TOKEN -- a guard worth keeping literal.
        self.workspace_secret = (
            os.environ.get("AGENTOS_WORKSPACE_SECRET") or "conductor-secrets")
        # Where the credential broker answers. Not a secret.
        self.control_panel_url = os.environ.get("CONTROL_PANEL_URL") or ""
        # The FULL proxy port (3128), not the store's (3129): a criterion
        # may reach the object store and nothing else, an executor needs
        # the forge and a package index. Spelled out rather than left to
        # inheritance, because the conductor has no proxy variables of
        # its own -- it makes no outbound calls on a model's behalf.
        self.egress_proxy = (os.environ.get("AGENTOS_EGRESS_PROXY")
                             or "http://egress-proxy:3128")
        # A THIRD IDENTITY, and the reason is the tree.
        #
        # A criterion's pod has to read one object out of the artifact
        # store, so whatever it runs as needs `storage.objects.get`.
        # Giving that to `agentos-execution` would hand the EXECUTOR
        # bucket read as well — the same KSA, the same Workload Identity
        # — and "execution holds nothing" is the property the whole pod
        # design rests on. The verifier's Jobs run as somebody else.
        #
        # A CUSTOM ROLE, because `roles/storage.objectViewer` includes
        # `storage.objects.list` and with list the names are enumerable:
        # a criterion could walk the bucket and read every other story's
        # tree. Without it the hash is the capability. See
        # scripts/infra/gke_cluster.sh.
        self.verifier_service_account = (
            os.environ.get("AGENTOS_K8S_VERIFIER_SERVICE_ACCOUNT")
            or "agentos-verifier")
        # A FOURTH, because read and write are different capabilities.
        # The replay WRITES what it captured; a criterion only reads.
        # Sharing one identity would give every criterion the ability to
        # put objects where the control panel and the `artifact`
        # criterion will read them.
        self.capture_service_account = (
            os.environ.get("AGENTOS_K8S_CAPTURE_SERVICE_ACCOUNT")
            or "agentos-capture")
        self._core = None

    def preflight(self) -> None:
        """Is this driver usable at all? Raise if not.

        CALLED AT STARTUP, and the reason is an hour of logs. The client
        is imported lazily — correctly, so that E5's module-load check
        passes on an image with no cloud SDK — and the consequence is
        that nothing touches the import until the first API call. The
        conductor therefore deployed to GKE, rolled out 2/2, logged
        `driver=KubernetesDriver`, and failed every reconcile pass with
        ModuleNotFoundError once a minute while reporting Ready.

        A driver that cannot make a single call is a deployment fault,
        and a deployment fault should stop the deployment. `kubectl
        rollout status` then fails, which is what the deploy already
        waits on.
        """
        _api()

    def _core_api(self):
        if self._core is None:
            client, config = _api()
            try:
                config.load_incluster_config()
            except Exception:                            # noqa: BLE001
                # A laptop with a kubeconfig. In-cluster first because
                # that is where this runs; falling back the other way
                # would make a misconfigured pod quietly talk to
                # whatever cluster the image happened to ship a config
                # for.
                config.load_kube_config()
            self._core = client.CoreV1Api()
        return self._core

    # ── lifecycle ────────────────────────────────────────────────────

    async def create(self, spec: ExecutionSpec, network_alias: str = "",
                     labels: dict | None = None) -> str:
        """The PVC then the pod. Returns the pod name."""
        validate_spec(spec)
        # Refused BY NAME before anything is created. A capability
        # quietly dropped here is a pod the caller believes is sandboxed
        # and is not.
        require_supported(spec, "kubernetes", self.CAPABILITIES)

        name = f"exec-{spec.execution_id}" if spec.execution_id is not None \
            else f"exec-{os.urandom(4).hex()}"
        # Through `claim_name` when there is an id, because the reclaim
        # pass parses that id back OUT of this name -- so the spelling
        # here and the spelling there have to be one fact, not two that
        # agree today. Without an id the claim is unparseable and
        # nothing will ever reclaim it; no production path reaches that
        # branch (every caller of `spec_for_dispatch` passes an int),
        # and a sweep that deleted disks whose owner it could not read
        # would be the worse failure.
        pvc = claim_name(spec.execution_id) if spec.execution_id is not None \
            else f"ws-{name}"
        api = self._core_api()

        await self._ensure_pvc(api, pvc_manifest(spec, pvc,
                                                 namespace=self.namespace))
        body = pod_manifest(spec, name, namespace=self.namespace,
                            pvc_name=pvc,
                            service_account=self.service_account,
                            # Refused HERE, before anything is created,
                            # for the reason `require_supported` above
                            # is: a pod built without the bridge starts
                            # a sandbox whose command is a path that
                            # does not exist, and the failure arrives
                            # later as a tool call that times out.
                            bridge_image=k8s_bridge.kernel_image(),
                            # Deployment facts, resolved here and not in
                            # the manifest builder. The secret is NAMED,
                            # never read: Kubernetes hands its value to
                            # the one container that clones, so the
                            # bearer is in no spec, no manifest and no
                            # `kubectl describe` output.
                            workspace_secret=self.workspace_secret,
                            control_panel_url=self.control_panel_url,
                            egress_proxy=self.egress_proxy,
                            # D1: the pod's OWN database connection, so
                            # the leg's advisory lock can move with the
                            # leg in D2. Resolved HERE for the same
                            # reason the two above are -- which instance
                            # and which Secrets is a deployment fact, and
                            # `pod_manifest` is a pure function of its
                            # arguments. None when the deployment has not
                            # been given a key, which is E0, a laptop and
                            # `next` before a person creates one: the pod
                            # is then exactly what it is today.
                            sql=k8s_sqlproxy.from_env(),
                            labels=labels)
        await self._ensure_pod(api, body, name)
        logger.info("pod %s created (isolation=%s node=%s)",
                    name, spec.isolation, spec.node_class,
                    extra={"event": "dispatch"})
        return name

    async def _ensure_pvc(self, api, body) -> None:
        """Create, or accept that it is already there.

        A resumed execution re-attaches the SAME claim — that is what
        makes a preempted pod recoverable — so "already exists" is the
        ordinary case on the path that matters most, not an error.
        """
        try:
            await call(api.create_namespaced_persistent_volume_claim,
                       namespace=self.namespace, body=body)
        except Exception as exc:                         # noqa: BLE001
            if not _already_exists(exc):
                raise

    async def _ensure_pod(self, api, body, name: str) -> None:
        try:
            await call(api.create_namespaced_pod,
                       namespace=self.namespace, body=body)
        except Exception as exc:                         # noqa: BLE001
            if not _already_exists(exc):
                raise
            logger.info("pod %s already exists; adopting it", name)

    async def destroy(self, container_id: str, stop_timeout: int = 0) -> None:
        """Delete the POD. The claim is the execution's, not the pod's.

        Deleting the claim here would destroy the workspace every time a
        Spot pod was preempted, which is the exact scenario the claim
        exists to survive.
        """
        api = self._core_api()
        try:
            await call(api.delete_namespaced_pod,
                       name=container_id, namespace=self.namespace,
                       grace_period_seconds=stop_timeout)
        except Exception as exc:                         # noqa: BLE001
            if not _not_found(exc):
                raise
            # Already gone is the end state we wanted. Same rule the
            # gateway path uses for agents.delete.

    async def await_ready(self, container_id: str,
                          timeout: float = 120.0,
                          bridge_port: int | None = None) -> bool:
        """Both containers Ready, or raise.

        Phase alone is not enough: a pod is `Running` the moment its
        containers start, and the bridge shim is not listening yet. The
        readiness condition is what the worker actually needs.

        `bridge_port` IS ACCEPTED AND NOT USED, and both halves are
        deliberate. `dispatch_admitted` passes it for every driver --
        Docker needs it because a host runs many sandboxes and each
        publishes its own port. A pod runs exactly one, on a fixed port,
        so there is nothing here to tell apart.

        Taking it anyway is the point. Without the parameter this raised
        `TypeError: await_ready() got an unexpected keyword argument`
        AFTER the pod was created -- so dispatch's cleanup destroyed it,
        the claim survived (as it must, for preemption), and the next
        tick built the pod again. Three pods created and destroyed per
        minute, two orphaned claims against a quota of 40, and the run
        stuck in DISPATCHING. A shared call site cannot carry an
        argument only some implementations accept.

        TWO MINUTES, NOT THIRTY SECONDS. This pod now runs two init
        containers before either of ours starts: one copies the bridge,
        one CLONES THE REPOSITORIES. Thirty seconds was right for a pod
        that only had to pull an image; a clone of any real repository
        is not reliably done in it, and the timeout would have read as
        "the pod never became ready" rather than "the workspace was
        still being built".
        """
        import asyncio

        api = self._core_api()
        deadline = asyncio.get_event_loop().time() + timeout
        last = "unknown"
        while asyncio.get_event_loop().time() < deadline:
            pod = await call(api.read_namespaced_pod, name=container_id,
                             namespace=self.namespace)
            last = pod.status.phase
            if last in ("Failed", "Succeeded"):
                # WITH THE REASON, because the pod does not survive this.
                # `dispatch_admitted` destroys it when this raises, and
                # the log that says what actually failed goes with it --
                # so the one chance to carry the cause out is here,
                # while the pod object is still in hand. Five dispatches
                # of execution 6 died in `workspace-compose` and this
                # sentence was all any of them said.
                raise RuntimeError(
                    f"pod {container_id} reached {last} before becoming "
                    f"ready — it exited rather than started"
                    f"{_exit_detail(pod)}")
            statuses = pod.status.container_statuses or []
            names = {c.name for c in statuses if c.ready}
            if {WORKER, SANDBOX} <= names:
                return True
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"pod {container_id} not ready after {timeout}s "
            f"(phase={last}{_init_detail(pod)})")

    async def list_containers(self) -> list[dict]:
        """Every pod this kernel owns, in the driver's shared shape."""
        api = self._core_api()
        pods = await call(api.list_namespaced_pod,
                          namespace=self.namespace,
                          label_selector=LABEL_SELECTOR)
        return [{"id": p.metadata.name,
                 "labels": dict(p.metadata.labels or {}),
                 "status": p.status.phase}
                for p in pods.items]

    async def list_workspaces(self) -> list:
        """Execution ids whose claim this namespace still holds.

        THE RECLAIM PASS IS DRIVEN FROM HERE and not from the executions
        table, because the table cannot see this. `destroy()` nulls
        `container_id` and deliberately keeps the claim, so a finished
        execution looks -- to any query over executions -- exactly like
        one that never had a workspace. The first version of the sweep
        asked for `container_id IS NOT NULL` and therefore skipped every
        row it existed to collect.

        THE NAME CARRIES THE ID, so a claim whose name does not parse is
        left alone: `podcheck` writes `ws-podcheck-<ts>` through the same
        manifest builder and wears the same label, and an operator's
        hand-made claim should not be deleted by a sweep either. Reaping
        on the label alone would take both. `workspace_execution` is that
        rule, stated with the other reapers' in `kernel/ownership.py`.
        """
        api = self._core_api()
        claims = await call(api.list_namespaced_persistent_volume_claim,
                            namespace=self.namespace,
                            label_selector=LABEL_SELECTOR)
        held = []
        for claim in claims.items:
            eid = workspace_execution(claim.metadata.name or "")
            if eid is not None:
                held.append(eid)
        return held

    async def release_workspace(self, execution_id: int) -> bool:
        """Delete the execution's claim. The other half of `destroy`.

        Called only from the reclaim pass, which reaches an execution
        after its story is done or reviewed, its run was cancelled, or
        it has been FAILED and idle for two hours -- never on
        preemption, which is exactly what `destroy` keeping the claim
        protects.

        ALREADY GONE IS SUCCESS, the same rule `destroy` applies to a
        missing pod: a reclaim that raises on a claim somebody deleted
        by hand would stop the sweep reaching the next one, and a single
        wedged workspace would hold a 40-claim quota closed for
        everything behind it.
        """
        name = claim_name(execution_id)
        api = self._core_api()
        try:
            await call(api.delete_namespaced_persistent_volume_claim,
                       name=name, namespace=self.namespace)
        except Exception as exc:                         # noqa: BLE001
            if _not_found(exc):
                return False
            raise
        logger.info("claim %s reclaimed", name, extra={"event": "reconcile"})
        return True

    async def resolve_bridge_port(self, container_id: str) -> int | None:
        """Fixed. One sandbox per pod means one port."""
        return BRIDGE_PORT

    async def resolve_bridge_host(self, container_id: str) -> str:
        """The pod's own address. There is no loopback route to it.

        The conductor is a separate pod, so the shim has to be reached
        by IP. Read from the API rather than cached at create: with two
        conductor replicas the one that dispatched is not necessarily
        the one that runs the next leg, and a cache miss would resolve
        to `localhost` and call the conductor's own loopback.

        Refuses rather than falling back for the same reason
        `resolve_bridge_port` refuses to assume 9100: a wrong address
        here produces tool calls that go somewhere real and answer
        wrongly, which is worse than a call that fails.
        """
        api = self._core_api()
        pod = await call(api.read_namespaced_pod, name=container_id,
                         namespace=self.namespace)
        ip = getattr(getattr(pod, "status", None), "pod_ip", None)
        if not ip:
            raise RuntimeError(
                f"pod {container_id} has no address yet, so its bridge "
                f"cannot be reached. A leg dispatched against it would "
                f"call the conductor's own loopback.")
        return ip

    def sandbox_attachments(self, workspace_container_path: str,
                            workspace_subpath: str = "") -> dict:
        """REFUSED, and the refusal is the point.

        On Docker the worker creates the sandbox as a second container
        and needs to know what to mount into it — which host paths, which
        volume, which network namespace to join. `sandbox_attachments`
        answers that by inspecting itself through the Docker API.

        Here the sandbox is created WITH the pod, by `create`, from the
        manifest. There is nothing to attach after the fact and no host
        path to discover. A caller reaching this has assumed the Docker
        process model — one long-lived worker spawning sandboxes on
        demand — which a pod's fixed container list cannot provide.

        Returning empty binds would let that caller proceed and produce a
        sandbox with no workspace, which fails later and somewhere else.
        """
        raise NotImplementedError(
            "sandbox_attachments has no meaning on Kubernetes: the sandbox "
            "is a container in the execution's own pod, created with it. "
            "A caller needing one has assumed the Docker process model.")

    # ── the verifier ─────────────────────────────────────────────────
    # Delegated to k8s_ephemeral. A criterion is a Job, not a pod, and
    # runs as a different identity; keeping it here would make this file
    # two subjects.

    async def run_ephemeral(self, spec: ExecutionSpec, timeout: float = 300.0,
                            upload=None, also_upload=None,
                            services: list | None = None,
                            fetch: list | None = None) -> dict:
        """One criterion, as a Job. See k8s_ephemeral.run_ephemeral."""
        from kernel.drivers.k8s_ephemeral import run_ephemeral

        return await run_ephemeral(self, spec, timeout=timeout, upload=upload,
                                   also_upload=also_upload, services=services,
                                   fetch=fetch)

    async def claim_exists(self, claim: str) -> bool:
        """Is that PVC there? See k8s_verify.claim_exists."""
        from kernel.drivers.k8s_verify import claim_exists

        return await claim_exists(self, claim)

    async def run_job_on_claim(self, spec: ExecutionSpec, *, claim: str,
                               timeout_s: float, tree_source: str,
                               tree_token: str) -> dict:
        """The prepare Job. See k8s_verify.run_job_on_claim."""
        from kernel.drivers.k8s_verify import run_job_on_claim

        return await run_job_on_claim(self, spec, claim=claim,
                                      timeout_s=timeout_s,
                                      tree_source=tree_source,
                                      tree_token=tree_token)

    async def run_ephemeral_capture(self, spec: ExecutionSpec, capture: str,
                                    timeout: float = 300.0,
                                    upload=None, fetch=None) -> dict:
        """Run, then take a directory away. See k8s_ephemeral.run_capture."""
        from kernel.drivers.k8s_ephemeral import run_capture

        return await run_capture(self, spec, capture, timeout=timeout,
                                 upload=upload, fetch=fetch)


    async def create_network(self, name: str, internal: bool = True) -> str:
        """REFUSED, and the replacement is better rather than equivalent.

        The Docker driver builds a private network per verification so a
        declared service — a database a criterion needs — is reachable by
        that criterion and nothing else. A pod does not need one: its
        network namespace ALREADY is a private network. A declared
        service becomes a sidecar in the criterion's own Job pod,
        answering on localhost, free and impossible to misconfigure into
        admitting a second criterion. `k8s_job.service_sidecar` builds it.

        `kernel/verifier/services.py` starts services separately and
        BEFORE the criterion, so adopting this needs that seam changed —
        which is why this refuses instead of returning a fake handle that
        would leave every declared service unreachable.
        """
        raise NotImplementedError(
            "create_network has no Kubernetes equivalent: a declared "
            "service is a sidecar in the criterion's own pod, where the "
            "pod's namespace is the private network. See "
            "kernel/drivers/k8s_job.py:service_sidecar. Adopting it needs "
            "kernel/verifier/services.py to place services in the Job "
            "rather than start them beside it.")

    async def remove_network(self, network_id: str) -> None:
        """Nothing to remove: see create_network."""
        return None

    async def aclose(self) -> None:
        self._core = None


def _init_detail(pod) -> str:
    """Which init container is holding the pod, if one is.

    A pod that never becomes Ready is almost always waiting on an init
    container -- the bridge copy or the clone -- and `phase=Pending`
    says none of that. Naming the container and its state turns "the
    pod did not start" into "the workspace is still cloning" or "the
    clone failed", which are different problems with different fixes.
    """
    for status in (getattr(pod.status, "init_container_statuses", None) or []):
        if getattr(status, "ready", False):
            continue
        state = getattr(status, "state", None)
        for kind in ("waiting", "terminated"):
            at = getattr(state, kind, None)
            if at is not None:
                reason = getattr(at, "reason", None) or kind
                return f", init {status.name}: {reason}"
        return f", init {status.name}: running"
    return ""


def _exit_detail(pod) -> str:
    """Which container died and what it last said.

    The companion to `_init_detail`: that one explains a pod still
    WAITING, this one a pod that is already over. Both exist because
    "the pod did not become ready" is true of every failure and useful
    for none of them.

    The message comes from `terminationMessagePolicy:
    FallbackToLogsOnError`, stamped on every container by
    `k8s_spec._keep_the_reason` -- without it this reports an exit code
    and nothing else, which is where this started.

    Trimmed to the LAST few lines. A Python traceback's final line is
    the exception; the middle is frames a reader of a conductor log
    cannot act on, and the whole thing would push the real cause off
    the top of the message.
    """
    parts = []
    for key in ("init_container_statuses", "container_statuses"):
        for status in (getattr(pod.status, key, None) or []):
            at = getattr(getattr(status, "state", None), "terminated", None)
            if at is None or not getattr(at, "exit_code", 0):
                continue
            said = (getattr(at, "message", None) or "").strip()
            tail = " / ".join(said.splitlines()[-3:]) if said else \
                "no message (terminationMessagePolicy?)"
            parts.append(f"{status.name} exited "
                         f"{at.exit_code} ({getattr(at, 'reason', '')}): "
                         f"{tail}")
    return f" — {'; '.join(parts)}" if parts else ""


def _already_exists(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 409 or "AlreadyExists" in str(exc)


def _not_found(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404 or "NotFound" in str(exc)
