"""Driver — abstract substrate seam for execution containers.

One interface, multiple implementations:
  - DockerDriver  (C3, self-hosted and local tiers)
  - KubernetesDriver  (I2, scale tier)

Nothing above the driver may know which driver it is. No substrate
conditional in the Conductor, contracts, criteria, ledger, or ask
machinery. Substrate selection is one config value.

Seven methods: six seam operations + aclose for resource cleanup.
Four operations raise NotImplementedError until their increment lands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kernel.spec import ExecutionSpec


class Driver(ABC):
    """Abstract substrate driver. Subclasses implement create/await_ready/destroy;
    the remaining operations are stubs until their increments land."""

    # WHAT THIS SUBSTRATE CAN ACTUALLY PROVIDE, as {field: (values...)}.
    #
    # EMPTY MEANS "THE DEFAULT ONLY", and that direction is deliberate:
    # a capability added to kernel/spec.py tomorrow is refused by every
    # existing driver until one opts in, rather than silently ignored by
    # all of them. Ignoring is the dangerous default -- a driver that
    # quietly ran an `isolation="syscall"` spec on the host kernel hands
    # back a container the caller believes is sandboxed, and nothing
    # downstream can tell.
    #
    # Drivers declare, `kernel.spec.require_supported` enforces, and the
    # refusal names the field, the value and the substrate.
    CAPABILITIES: dict = {}

    # HOW A DECLARED SERVICE IS PROVIDED, and it is a real difference
    # rather than a naming one.
    #
    #   "network"  the substrate can build a private network and attach
    #              containers to it. The criterion reaches `db:5432` by
    #              name and the network denies everything outside it.
    #              Docker.
    #
    #   "sidecar"  it cannot, and does not need to: a pod's network
    #              namespace ALREADY is a private network, so the service
    #              goes in the criterion's own pod and answers on
    #              localhost. Kubernetes.
    #
    # `kernel/verifier/services.py` branches on this. It is stated as a
    # property rather than discovered by catching NotImplementedError,
    # because "this driver cannot make networks" and "creating the
    # network failed" are different facts and only one of them is fine.
    SERVICE_MODEL: str = "network"

    # CAN A FINISHED CONTAINER'S DIRECTORY BE READ BACK OUT.
    #
    #   "archive"  yes — `GET /containers/{id}/archive` before it is
    #              destroyed. Docker. This is what lets the environment
    #              replay install packages once and carry them to a
    #              criterion that has no network of its own.
    #
    #   "none"     no. A Job's pod cannot be read after it exits, and
    #              there is no Kubernetes equivalent. The replay would
    #              have to travel through the artifact store instead,
    #              which is a change to kernel/verifier/prepare.py and a
    #              grant the execution identity does not have.
    #
    # Declared so `build_deps` can report the honest outcome — a check
    # that could not be given its dependencies did not run, which is
    # `error` and never `fail` — instead of raising NotImplementedError
    # out of the middle of a verification.
    CAPTURE_MODEL: str = "archive"

    # HOW THE COMMITTED TREE REACHES A CRITERION.
    #
    #   "push"   the caller hands over bytes and the driver writes them
    #            into the container before it starts — `PUT
    #            /containers/{id}/archive`. Docker. `run_ephemeral`
    #            takes `upload=(dir, tar)`.
    #
    #   "fetch"  the container gets itself the tree, by id, from the
    #            artifact store. Kubernetes: a Job's pod cannot be
    #            written to before it runs, so an initContainer pulls
    #            the object and unpacks it into a volume the criterion
    #            shares. `run_ephemeral` takes `tree_id=` instead.
    #
    # STATED, not discovered by catching NotImplementedError, for the
    # reason SERVICE_MODEL gives. And the difference is not cosmetic: on
    # "fetch" the tree becomes an OBJECT with an id, which is what lets
    # a criterion be given its own tree and no route to anyone else's —
    # the hash is the capability, and `list` is withheld.
    TREE_MODEL: str = "push"

    # WHERE THE EXECUTION'S WORKSPACE IS COMPOSED.
    #
    #   "local"  the conductor builds it on a filesystem the sandbox
    #            will share -- a named volume on Docker, a directory in
    #            E0. `prepare_workspace` clones into it directly, and
    #            the sandbox mounts the same bytes.
    #
    #   "pod"    the workspace is a volume of the execution's OWN pod
    #            and nothing outside that pod can write to it. On
    #            Kubernetes the claim is ReadWriteOncePod precisely so
    #            the API server refuses a second writer, which means the
    #            conductor cannot compose it however much it would like
    #            to.
    #
    # DECLARED RATHER THAN DISCOVERED, for the reason TREE_MODEL gives,
    # and this one was discovered the expensive way: `prepare_workspace`
    # ran the "local" path on Kubernetes and died on
    #
    #   [Errno 13] Permission denied: '/workspace'
    #
    # -- which reads as a file-mode problem and is not one. The
    # conductor is a non-root container with no such volume, and had it
    # been root with one, it would have cloned into bytes the pod could
    # never see. A wrong answer that looks like a configuration mistake
    # costs more than a refusal that names the missing piece.
    # WHETHER A DEAD SANDBOX CAN BE REBUILT WHERE IT STANDS.
    #
    #   "attached"  the sandbox is a container the conductor creates
    #               beside the execution and attaches to its workspace
    #               volume. Docker. A dead bridge is recoverable in
    #               place: destroy that container, create another
    #               against the same volume, carry on. The run keeps
    #               its id, its legs and its tree.
    #
    #   "pod"       the sandbox is a container INSIDE the execution's
    #               own pod, created with it. Kubernetes. There is no
    #               such thing as attaching one to a running pod, and
    #               `sandbox_attachments` refuses by name. Recovery is
    #               to destroy the POD and let the dispatch pass build
    #               another; the claim survives that, so the workspace
    #               does too -- which is the whole reason `destroy()`
    #               keeps the claim.
    #
    # DECLARED, BECAUSE THE REFUSAL ARRIVES TOO LATE OTHERWISE. This is
    # the fourth caller to meet "sandbox_attachments has no meaning on
    # Kubernetes": the message is correct, well written, and reached at
    # a point where the only thing the caller can do is log it and give
    # up. Execution 9 did exactly that on every tick -- bridge dead,
    # rebuild refused, leg abandoned, re-dispatch -- climbing to leg 8
    # with zero steps. Asking the seam first turns that into a branch.
    SANDBOX_MODEL: str = "attached"

    WORKSPACE_MODEL: str = "local"

    # CAN THIS SUBSTRATE RUN A ONE-SHOT JOB AGAINST A NAMED CLAIM.
    #
    #   "none"  no. There is no persistent claim to prepare an
    #           environment onto, so there is nothing for such a Job to
    #           write to. Docker and E0: the tar path in
    #           kernel/verifier/prepare.py is what runs there.
    #
    #   "job"   yes. Kubernetes: a batch/v1 Job mounting the
    #           verification PVC, which is how plan 5's prepare step
    #           writes `.venv` and `node_modules` to `volume.ENV`
    #           without anything leaving the Job but an exit code.
    #
    # DECLARED, NOT DISCOVERED, for the reason TREE_MODEL gives, and
    # E8 had already made the call the expensive way round:
    # `prepare_volume._run` reached past this seam into
    # `k8s_run.run_job` and `kubernetes._api` directly, because no
    # driver method could do it. The reach-through worked and it meant
    # every caller of a substrate-agnostic function was one `getattr`
    # away from a traceback out of the middle of a verification --
    # which is how "this substrate has no volumes" comes to read as
    # "verification is broken". `build_deps` makes the same call about
    # captures and CAPTURE_MODEL is why it can.
    CLAIM_JOB_MODEL: str = "none"

    # WHERE `git archive` RUNS TO PRODUCE A CRITERION'S TREE.
    #
    #   "local"  the conductor's own subprocess, against a directory it
    #            can open. Docker and E0 -- the conductor composed the
    #            workspace, so the repository is right here.
    #
    #   "pod"    the repository is inside the execution's pod. The
    #            worker container runs the SAME `archive_committed_tree`
    #            and hands the tar back over kernel/workspace/podfs.py;
    #            nothing outside that pod can open the claim.
    #
    # SEPARATE FROM WORKSPACE_MODEL, though today one driver says "pod"
    # to both. That one is about who may WRITE the workspace; this is
    # about whether a commit can be READ out of it, and a substrate can
    # differ -- a read-only export beside a single-writer claim is the
    # obvious shape. One word meaning two things is how the next
    # substrate changes a behaviour nobody intended it to.
    #
    # DECLARED, not discovered by catching TreeExportError, for the
    # reason TREE_MODEL gives -- and here the exception actively lies.
    # `git -C /workspace archive` in the conductor's container is "not a
    # git repository", which `verify_claim` turns into "the committed
    # tree could not be archived" and every criterion reports
    # unrunnable. That reads as the executor having committed nothing,
    # when the tree is complete and sitting in another pod.
    ARCHIVE_MODEL: str = "local"

    async def list_workspaces(self) -> list:
        """Execution ids whose workspace this substrate still holds.

        Empty by default: Docker's workspace is a directory inside a
        volume the next run reuses, so there is nothing to enumerate and
        nothing to reclaim.

        THE RECLAIM SWEEP IS DRIVEN FROM THIS, not from the executions
        table, and the reason is a bug that reached the cluster: the
        first version asked for executions with a `container_id` and a
        terminal state, and `container_id` is nulled when the POD is
        destroyed while the CLAIM deliberately survives. So every row
        that actually needed reclaiming was the one excluded.
        """
        return []

    async def release_workspace(self, execution_id: int) -> bool:
        """Give back the execution's disk. Default: there is none to give.

        SEPARATE FROM `destroy`, AND IT HAS TO BE. `destroy()` means the
        POD, and its comment says why: "deleting the claim here would
        destroy the workspace every time a Spot pod was preempted, which
        is the exact scenario the claim exists to survive". Folding a
        reclaim into it would take the workspace away on every
        preemption -- the failure the claim was designed to prevent.

        WHAT NOT CALLING THIS COSTS. Nothing ever did. `_ensure_pvc`
        creates one per execution, `destroy` keeps it, the orphan sweep
        touches only pods, and the namespace quota is
        `persistentvolumeclaims: "40"`. So the fortieth execution is not
        the fortieth concurrent one, it is the fortieth EVER -- and
        after it, nothing in the environment can run a story.

        True when something was actually reclaimed, so a caller can log
        the difference between "released" and "there was nothing to
        release". Docker's workspace is a directory inside a volume the
        next run reuses; there is no claim, and no-op is the honest
        answer rather than a silent success.
        """
        return False

    def preflight(self) -> None:
        """Raise if this driver cannot make a single call. Default: fine.

        A substrate whose client is imported lazily -- which is every
        cloud one, so that an image without the SDK can still import
        every module under kernel/ -- cannot discover its own absence
        until something calls it. That turns a deployment fault into a
        runtime one, repeated for ever, while the pod reports Ready.
        """

    @abstractmethod
    async def create(self, spec: ExecutionSpec) -> str:
        """Create and start an execution container from the spec.

        Returns the substrate-specific container/pod ID.
        Must call validate_spec(spec) BEFORE any substrate API call.
        """

    @abstractmethod
    async def await_ready(self, container_id: str, timeout: float = 30.0) -> bool:
        """Poll until the container is ready, or raise on timeout/early exit."""

    @abstractmethod
    async def list_containers(self) -> list[dict]:
        """List all managed containers.

        Returns list of {"id": str, "labels": dict, "status": str}.
        Only containers with the "managed-by=kernel" label are included.
        The K8s driver at I2 implements the same contract.
        """

    async def exec(self, container_id: str, tool: str, args: dict,
                   bridge_port: int | None = None,
                   bridge_host: str = "") -> dict:
        """Execute a tool call inside the container via the bridge.

        Implemented via the localhost bridge (ES3 cycle-07), NOT docker exec.
        The socket proxy denies /exec/{id}/start (B3). Tool calls reach the
        sandbox over the bridge shim (shared netns).
        Do not set EXEC=1 on the proxy to 'fix' a 403 — that undoes B3.

        bridge_port: the port the shim is listening on for this container.
        **Resolved from the container when omitted** (H1c). Under a shared
        netns each sandbox needs its own port, and the old default of 9100
        meant two concurrent executions both addressed the first sandbox —
        the second execution's tool calls answered by the first one's shim,
        with nothing to indicate it. The label written at creation is the
        record; this reads it rather than recomputing, so a port that was
        reallocated under a race is still found.

        The BridgeToolExecutor (kernel/harness/executor.py) is the primary
        interface for the model loop. This driver method is the lower-level
        substrate interface used by the executor.
        """
        from kernel.bridge.client import call_bridge
        from kernel.bridge.limits import bridge_timeout

        if bridge_port is None:
            bridge_port = await self.resolve_bridge_port(container_id)
        if bridge_port is None:
            raise RuntimeError(
                f"container {container_id} has no bridge port label and none "
                "was given. Refusing to assume 9100: under a shared netns "
                "that reaches whichever sandbox got there first."
            )
        # THE HOST, RESOLVED THE SAME WAY THE PORT IS. `call_bridge`
        # defaults to localhost, which is right on Docker -- the shim
        # shares the worker's netns -- and addresses the CONDUCTOR on
        # Kubernetes, where the shim is in another pod. Resolved rather
        # than defaulted, for the reason the port above gives: a wrong
        # answer here reaches something, and what it reaches is not the
        # sandbox this call is for.
        if not bridge_host:
            bridge_host = await self.resolve_bridge_host(container_id)
        # The caller's own ceiling, not the 30s default. A `bash` whose
        # args ask for 120s and whose HTTP read gives up at 30 reports a
        # dead bridge for a command that was working -- see
        # kernel/bridge/limits.py, and execution 200, where this cost a
        # whole run.
        result = await call_bridge(tool, args, f"exec-{container_id}-{tool}",
                                   port=bridge_port, host=bridge_host,
                                   timeout=bridge_timeout(tool, args))
        return {
            "output": result.output,
            "error": result.error,
            "exit_code": result.exit_code,
        }

    async def snapshot(self, container_id: str) -> dict:
        """Capture execution state for checkpointing.

        NOT implemented until E3 (harness-owned workspace snapshots).
        """
        raise NotImplementedError(
            "snapshot: not implemented until E3 "
            "(harness-owned workspace snapshots)."
        )

    async def resolve_bridge_host(self, container_id: str) -> str:
        """WHERE this sandbox's shim answers. Default: the caller's own host.

        Docker and E0 put the sandbox in the CALLER'S network namespace,
        so `localhost` is both correct and the tightest reach there is —
        the shim binds 127.0.0.1 precisely so nothing else can call it.

        A pod substrate cannot have that: the sandbox is a container in
        a DIFFERENT pod, and `localhost` from the conductor answers
        nothing. `call_bridge` hardcoded it, so every tool call on
        Kubernetes went to the conductor's own loopback.

        Concrete and not abstract, because every driver that shares a
        namespace is already right and making them all say so would be
        ceremony. A driver that does not share one overrides it.
        """
        return "localhost"

    @abstractmethod
    async def resolve_bridge_port(self, container_id: str) -> int | None:
        """The bridge port recorded on this sandbox, or None if unlabelled."""
        ...

    @abstractmethod
    def sandbox_attachments(self, workspace_container_path: str,
                            workspace_subpath: str = "") -> dict:
        """Binds for a sandbox. `workspace_subpath` NARROWS the workspace.

        A driver that ignores the subpath mounts the whole shared volume,
        which is how execution 1544 deleted its own workspace and the
        seed it was cloned from with a relative `rm -rf` -- its cwd sat
        one level above its own tree. Every implementor must honour it.
        """
        """How a sandbox reaches the bridge and the workspace, per substrate.

        ONLY THE DRIVER CAN ANSWER THIS. Docker replies with bind strings
        built from host paths it must ask the daemon for — the worker knows
        `/app/kernel`, not where that lives outside the container. Kubernetes
        replies with a volume reference and no host path exists at all,
        because the workspace is a PVC and the sandbox is a container in the
        same pod.

        This method exists because the first version of H1a put that
        discovery in `kernel/conductor/`, which calls the Docker API from
        the substrate-agnostic layer — code that would run locally and mean
        nothing on I2.

        Returns a dict the conductor passes through to ExecutionSpec:
        {"binds": [...], "network_mode": str}. A driver with no host paths
        returns whatever its substrate needs; the conductor does not read
        the contents, only forwards them.
        """
        ...

    @abstractmethod
    async def destroy(self, container_id: str) -> None:
        """Stop and remove the container. Idempotent — tolerates already-gone."""

    async def claim_exists(self, claim: str) -> bool:
        """Is there a persistent claim by this name on this substrate?

        THE OTHER HALF OF `run_job_on_claim`, and unowned until now.
        E17 hoisted the RUN onto this seam and left the QUESTION behind:
        `kernel/verifier/handover.has_claim` still reached through to
        `driver._core_api()` and imported `kubernetes._not_found` from
        inside the verifier -- a substrate's private module, called by a
        function every substrate calls. Plan 5.17 item 11 names it.

        WHY IT MATTERS THAT THIS IS ASKED AND NOT TRIED. The question
        gates everything the rebuild gate spends: a prepare Job pointed
        at a claim that does not exist FAILS, and 5.8 row 5 turns a
        failed prepare into `error` on every criterion -- telling an
        executor its manifest is broken because a PVC was never
        provisioned. Getting a traceback out of the middle of that,
        instead of a sentence, is how "this substrate has no volumes"
        comes to read as "verification is broken".

        ASK `CLAIM_JOB_MODEL` FIRST, exactly as for `run_job_on_claim`.
        A substrate with no claims refuses here with a sentence rather
        than pretending to answer: "there are no claims on this
        substrate" and "that claim is gone" are different facts, and
        answering the first with `False` would let a caller conclude the
        second.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares CLAIM_JOB_MODEL="
            f"{self.CLAIM_JOB_MODEL!r}, so it has no persistent claims to "
            f"ask about. Callers must ask CLAIM_JOB_MODEL and report the "
            f"refusal -- 'this substrate has no volumes' and 'that claim "
            f"does not exist' are different facts.")

    async def run_job_on_claim(self, spec: ExecutionSpec, *, claim: str,
                               timeout_s: float, tree_source: str,
                               tree_token: str) -> dict:
        """Run `spec` to completion as a Job with `claim` mounted.

        THE VERIFIER'S PREPARE STEP, AND THE ONLY CALLER SO FAR. Plan
        5.15: the Job pulls the committed tree from the execution pod's
        workspace surface onto the claim, then runs the repository's own
        setup against it. `tree_source` says where that tree is
        (`kernel/verifier/prepare_source.py` builds and parses it) and
        `tree_token` is the READ-ONLY workspace token that buys it --
        two arguments and not one, so that logging the first cannot leak
        the second.

        Returns `run_ephemeral`'s dict: exit_code, output, timed_out,
        infra_error. NEVER raises on a non-zero exit, for the reason
        `k8s_run` gives -- a failing install is DATA, and the caller
        turns it into `error` for every criterion (5.8 row 5).

        ASK `CLAIM_JOB_MODEL` FIRST. A substrate that cannot do this at
        all should be refused by the caller with a sentence, before
        anything is attempted; reaching here on such a driver is a bug
        in the caller and says so rather than pretending.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares CLAIM_JOB_MODEL="
            f"{self.CLAIM_JOB_MODEL!r}, so it cannot run a Job against a "
            f"persistent claim. Callers must ask CLAIM_JOB_MODEL and "
            f"report the refusal -- 'this substrate has no volumes' and "
            f"'the Job failed' are different facts.")

    async def run_ephemeral(self, spec: ExecutionSpec, commands: list) -> str:
        """Run a one-shot execution and return its output.

        NOT implemented until F2 (Verifier as ephemeral execution
        through the driver).
        """
        raise NotImplementedError(
            "run_ephemeral: not implemented until F2 "
            "(Verifier as ephemeral execution through the driver)."
        )

    async def aclose(self) -> None:
        """Release driver resources (HTTP clients, connections).

        Subclasses override; base is a no-op.
        """
