"""ExecutionSpec to a Kubernetes pod manifest. No SDK, no cluster.

WHY THIS IS ITS OWN MODULE. The translation is the part with opinions in
it — one pod or two, which container gets the workspace, what
`isolation="syscall"` spells — and the part worth testing on a laptop.
The driver around it is API calls. Splitting them means the decisions
below are asserted by unit tests that need no cluster, and the day the
client library changes, none of them move.

Pure dicts, deliberately: no `from kubernetes import client`. E0's image
has no SDK and E5's module-load imports every module under kernel/.

ONE POD, TWO CONTAINERS — see
docs/refactor/decisions/p1-one-pod-per-execution.md. The short version:
splitting them needs a ReadWriteMany workspace, which costs Filestore
and reintroduces the two-writer race ReadWriteOncePod exists to prevent,
and it turns the tool bridge into an authenticated network service when
today it is safe by being unreachable. The boundary is credential
possession, not network position.
"""

from __future__ import annotations

import os

from kernel.drivers import (k8s_bridge, k8s_placement, k8s_sqlproxy,
                            k8s_workspace)
from kernel.sizes import WORKER_CPUS, WORKER_MEMORY_MB
from kernel.spec import ExecutionSpec
from kernel.workspace import podfs_auth
from kernel.workspace.credentials import sandbox_env

# The word the capability contract uses -> what this substrate calls it.
# ONE PLACE. A `runtimeClassName` spelled at a call site would make every
# caller that wants a sandbox depend on Kubernetes, which is what
# kernel/spec.py exists to prevent.
RUNTIME_CLASS = {
    "shared": None,          # the node's default runtime
    "syscall": "gvisor",     # GKE Sandbox; free, and per-pod since 1.27.4
}

STORAGE_CLASS = {
    "standard": "standard-rwo",
    "fast": "premium-rwo",
}

NODE_CLASS_LABEL = "agentos.io/node-class"

# ── AND WHICH ENVIRONMENT'S COPY OF THAT POOL ───────────────────────────
#
# One cluster runs more than one environment now, and both carry pools
# labelled small/medium/large -- the class is a SHAPE, and a dev story
# needs the same shape a next story does. So the class alone stopped
# identifying a machine: without this key a dev pod and a next pod
# select the same nodes, one environment's runaway story starves the
# other, and it does so on hardware the other is paying for.
#
# NOTHING FAILS WHEN THAT HAPPENS, which is why it is worth a second
# label rather than a convention. Both pods run. They are simply on the
# wrong machines, and the only trace is a bill.
ENV_LABEL = "agentos.io/env"

# ── AND WHETHER THIS CLUSTER'S POOLS CAN SATISFY IT ────────────────────
#
# THE TWO HALVES OF THIS FEATURE SHIP SEPARATELY, which is the whole
# reason for a switch. The SELECTOR is in this image. The LABEL is on
# the nodes, and it gets there when a person runs `node-pools update`.
# An image that always emitted the selector, deployed to a cluster whose
# pools predate the label, does not fail: every pod is admitted,
# scheduled against a label no node carries, and sits Pending -- the one
# state that reports nothing on its own.
#
# So the deployment says whether its pools are labelled, and the
# selector is emitted only there. An environment that has not been
# migrated gets exactly what it got before this existed.
POOL_SCOPE_ENV = "AGENTOS_NODE_POOL_SCOPED"
POOL_SCOPE_OFF = "off"

WORKER = "worker"
SANDBOX = "sandbox"


def node_environment(namespace: str) -> str:
    """Which environment a pod belongs to, from the namespace it is in.

    DERIVED, NOT CONFIGURED, and that is the whole argument for this
    shape. The alternative was a second variable beside
    AGENTOS_K8S_NAMESPACE saying the same thing twice, and two values
    that must agree are two values that eventually do not. A pod in
    `agentos-dev` selecting next's pool is not an error anything
    reports; it is a story running on the wrong bill.

    It costs no plumbing: `pod_manifest`, `job_manifest` and
    `prepare_manifest` each already take `namespace` as a keyword
    argument, because each writes it into the object's metadata.

    REFUSED RATHER THAN GUESSED when the namespace is not
    `agentos-<env>`. A wrong answer here is the quietest failure the
    platform has -- the pod is admitted, scheduled against a label no
    node carries, and stays Pending for ever. `conductor/unscheduled.py`
    exists because that state reports nothing on its own, so the place
    to catch it is before the object is built.
    """
    prefix = "agentos-"
    if not namespace.startswith(prefix) or len(namespace) == len(prefix):
        raise ValueError(
            f"namespace {namespace!r} is not 'agentos-<env>', so the pool "
            f"this pod belongs to cannot be derived from it. A pod given "
            f"the wrong {ENV_LABEL} does not fail -- it stays Pending "
            f"for ever -- which is why this refuses rather than guesses.")
    return namespace[len(prefix):]


def pools_are_env_scoped() -> bool:
    """Whether this deployment's node pools carry `agentos.io/env`.

    OFF IS THE DEFAULT, AND OFF IS EXACTLY THE OLD BEHAVIOUR -- the pod
    carries `agentos.io/node-class` and nothing else, byte for byte what
    it carried before any of this existed. An environment whose pools
    were built before the label keeps scheduling as it always did, and
    keeps doing so through a deploy of this image.

    Read per call rather than at import, and by MEMBERSHIP rather than
    `environ.get(name, default)`, for the reason
    `k8s_sqlproxy.from_env` gives: a default nobody can see is a policy
    nobody reviewed.

    THIS SWITCH IS MEANT TO DIE. It exists while one cluster is
    half-migrated -- one environment's pools labelled, another's not.
    When every pool carries the label there is nothing left for it to
    express, and the selector becomes unconditional again.
    """
    if POOL_SCOPE_ENV not in os.environ:
        return False
    value = os.environ[POOL_SCOPE_ENV].strip().lower()
    return bool(value) and value != POOL_SCOPE_OFF


def node_selector(node_class: str, namespace: str) -> dict:
    """The labels that together name one pool.

    THE CLASS ALWAYS, AND THE ENVIRONMENT WHERE THE POOLS CARRY IT.
    The same argument `pod_manifest` already makes about never skipping
    the selector applies to the class half here: a pod carrying no class
    lands on whatever has room.

    The environment half is conditional only because the nodes are
    migrated by hand, on a different schedule from the image. Where the
    pools do carry it, leaving it off is the expensive failure: a pod
    with only the class matches that class's pool in EVERY environment
    on the cluster and lands on whichever has room, so one
    environment's story quietly takes a whole machine another
    environment is paying for.
    """
    selector = {NODE_CLASS_LABEL: node_class}
    if pools_are_env_scoped():
        selector[ENV_LABEL] = node_environment(namespace)
    return selector


def node_tolerations(namespace: str) -> list:
    """The other half of the pair, and it is a PAIR.

    The selector says which pools this pod MAY use. It cannot say
    anything about who else may use them -- an environment whose pods
    carry no env selector matches every pool of its class, including
    this one's. So the pools are TAINTED as well as labelled, and this
    is what lets our own pods past the taint.

    WHY A TAINT RATHER THAN LABELLING BOTH SIDES. Symmetry would work:
    label every environment's pools, put the selector on every
    environment's pods. It also requires changing the OTHER
    environment -- relabelling live pools, which rolls them, which
    evicts running stories, which costs a leg off the convergence
    ceiling for work that was hours in. A taint on the NEW pools
    achieves the same isolation in both directions and touches nothing
    that is already running.

    SO THE TWO ARE NEVER SEPARATE. A pool labelled and not tainted lets
    the other environment in; a pool tainted and not labelled keeps our
    own pods out. `place_on_pool` applies both, and the parametrised
    test walks all three pod kinds, because the way this breaks is
    somebody adding a fourth builder and remembering only the selector.
    """
    if not pools_are_env_scoped():
        return []
    return [{"key": ENV_LABEL, "operator": "Equal",
             "value": node_environment(namespace), "effect": "NoSchedule"}]


def place_on_pool(pod_spec: dict, node_class: str, namespace: str) -> None:
    """Both halves, onto a pod spec, so neither can be applied alone.

    EMPTY IS OMITTED, NOT WRITTEN. An unscoped environment's pod gets
    no `tolerations` key at all rather than `tolerations: []` -- the
    two behave identically to Kubernetes, and the point is that the
    manifest is byte-for-byte what it was before any of this existed.
    """
    pod_spec["nodeSelector"] = node_selector(node_class, namespace)
    tolerations = node_tolerations(namespace)
    if tolerations:
        pod_spec["tolerations"] = tolerations
WORKSPACE_PATH = "/workspace"


def _resources(spec: ExecutionSpec, sandbox: bool = True) -> dict:
    """Requests only, and they are also the limits on Autopilot.

    Autopilot bills THE REQUEST and silently raises an out-of-ratio one:
    ask for 1 vCPU with 16GiB and CPU is lifted to ~2.46 and charged.
    The size ladder is 1:4 throughout for that reason, so anything built
    here from cpus/memory_mb should already be in ratio — this does not
    enforce it, because a spec that is out of ratio is a spec problem and
    validate_spec is where spec problems belong.

    THE TWO CONTAINERS ARE NOT THE SAME SIZE, and giving them the same
    one is what made a small run expensive. This took `spec` alone and
    was called twice, so a pod cost 2x the tier: an errand asking for
    1 vCPU reserved 2, and the second was a worker that clones, commits
    and answers HTTP and has never needed it. Against a 64-CPU namespace
    that halved how many runs fit.

    So the SPEC's numbers are the sandbox's — where installs, builds and
    test runs happen — and the worker takes kernel.sizes.WORKER_*, which
    is the same on every rung because none of what it does scales with
    the size of the work. `sandbox=` mirrors `_security_context`, which
    already discriminates the two containers this way.
    """
    cpus = spec.cpus if sandbox else WORKER_CPUS
    memory_mb = spec.memory_mb if sandbox else WORKER_MEMORY_MB
    return {
        "requests": {"cpu": str(cpus), "memory": f"{memory_mb}Mi",
                     "ephemeral-storage": f"{_ephemeral_gib(sandbox)}Gi"},
        "limits": {"cpu": str(cpus), "memory": f"{memory_mb}Mi",
                   "ephemeral-storage": f"{_ephemeral_gib(sandbox)}Gi"},
    }


# The workspace is a PVC and is not this. `ephemeral-storage` is the
# node's own disk: the container's writable layer, its /tmp, and every
# emptyDir the pod mounts -- which is where pip's build directory, npm's
# cache and a test suite's scratch files actually land.
#
# UNREQUESTED IS NOT UNUSED, which is the failure this closes. With no
# request, the scheduler believes the pod needs no disk and places it
# anywhere; the pod then fills the node's 100 GiB boot disk between
# them, and the kubelet evicts whichever pod it decides is the worst
# offender. The evicted pod did nothing wrong and the story that caused
# it may not be the story that dies. Requesting it makes the disk a
# scheduled resource like the other two, so the node is chosen knowing
# the cost and the pod is only evicted for exceeding its OWN limit.
#
# 30 GiB for the sandbox and 4 for the worker, against a 100 GiB node
# disk of which roughly 90 is usable. One story per machine means one
# sandbox per machine, so the sandbox may be generous; the worker
# clones, commits and answers HTTP and has never needed more.
SANDBOX_EPHEMERAL_GIB = 30
WORKER_EPHEMERAL_GIB = 4


def _ephemeral_gib(sandbox: bool) -> int:
    return SANDBOX_EPHEMERAL_GIB if sandbox else WORKER_EPHEMERAL_GIB


def spec_uid(spec: ExecutionSpec) -> int | None:
    """The numeric uid the spec declares, or None.

    ONE PLACE. `spec.user` is a Docker-shaped "uid:gid" string, and it
    is parsed by the execution pod's security context, by the verifier
    Job's, and by the initContainer that has to write files the
    criterion can read. Three parsers agreeing by coincidence is how
    two of them end up disagreeing.
    """
    head = (getattr(spec, "user", None) or "").split(":")[0]
    return int(head) if head.isdigit() else None


# The two uids, and the group they share.
#
# DISTINCT, for the reason `_security_context` gives: the sandbox must
# not be able to overwrite the worker's files. SHARING A GROUP, because
# both of them have to write the SAME workspace -- the model edits files
# through the sandbox and the worker commits them -- and two uids with
# no common group means one of them gets EACCES on a tree it owns half
# of.
SANDBOX_UID = 65532
WORKER_UID = 65531
WORKSPACE_GID = 65532


def _worker_uid(spec: ExecutionSpec) -> int:
    """The uid everything on the worker's side of the boundary runs as.

    ONE PLACE, for the reason `spec_uid` gives one line up. Three callers
    need this same "the contract's uid, or the floor": the worker's
    security context, the init container that composes a tree the worker
    must be able to commit, and the Cloud SQL proxy, whose socket is
    unopenable by a worker that is not its owner. They agreed by
    coincidence twice, which is how the third one comes to disagree.
    """
    uid = spec_uid(spec)
    return uid if uid is not None else WORKER_UID


def _proxy_env(egress_proxy: str) -> list:
    """THE ONLY WAY OUT OF THE SANDBOX, and it was never told.

    The namespace is default-deny egress; the single route to the
    internet is the squid Deployment on 3128, whose allowlist is the
    package indexes, the git hosts, the image registry and the model
    APIs. The INIT containers were given this -- which is why the clone
    worked -- and the sandbox was not.

    So `env_setup` ran `pip install -r requirements.txt` against a
    network with no route, and it did not fail: it HUNG, holding the
    bridge's tool lock, until every later call timed out and the
    executor reported "the tool bridge is unresponsive with TimeoutError
    on all operations" and parked to ask a person. Measured inside the
    pod: a direct connection to pypi.org never returns, and the same
    request through the proxy answers 200 in 0.1s.

    BOTH CASES, deliberately. curl reads lowercase `http_proxy`, most
    Python tooling reads either, npm reads both -- and a half-set pair
    is a tool that silently bypasses the proxy and hangs as before.

    NO_PROXY keeps loopback and cluster-internal names direct: the
    bridge talks to itself, and sending in-cluster traffic to an
    internet proxy would have it refused by the allowlist.
    """
    # DELEGATED, because three copies of this existed with three
    # different skip-lists and none named the metadata server -- which
    # is how a capture Job lost its identity. See k8s_egress.NO_PROXY.
    from kernel.drivers.k8s_egress import proxy_env  # noqa: PLC0415

    return proxy_env(egress_proxy)


def _credentials_env() -> list:
    """`AGENTOS_CREDENTIALS` for the sandbox -- and NOT spelled here.

    THE POD PATH NEVER GOT IT. `conductor/sandbox_spec.py` sets this on
    the Docker sandbox and has since Ec4d; the container env below is a
    closed list of literals and simply omitted it. So a goal holding a
    cloud grant ran on GKE with the lease written to
    `/workspace/.agentos/credentials` and nothing naming the directory:
    `tools_core._safe_env` had the variable in its allowlist and no
    variable to forward, and the model reported no credentials. What the
    operator sees is a permission error out of a cloud SDK, three layers
    away from the list that dropped it.

    A POINTER, NOT A SECRET, which is the only reason it may join a list
    whose rule is "nothing here opens anything". See
    `kernel/workspace/credentials.py`, whose entire argument is that the
    token lives in a file read at the moment of use and
    `AGENTOS_CREDENTIALS` is a path -- and a path is safe to print.

    DERIVED, NOT RESTATED. `workspace.credentials.sandbox_env` is the one
    function that computes this -- name and path together -- and it is
    what the Docker path reaches too: `conductor/sandbox_spec.py`'s
    `_credentials_pointer` is a one-line forward to it. Called directly
    rather than through that forward for two reasons: a driver importing
    the conductor inverts the layering this package exists to keep, and
    the forward is private, so routing through it would mean renaming a
    function whose name is already spelled in another test. Either way
    the lease directory is declared once, in `credentials.py`.
    """
    return [{"name": name, "value": value}
            for name, value in sandbox_env(WORKSPACE_PATH).items()]


def _timings_env(spec: ExecutionSpec) -> list:
    """The sandbox's budgets, resolved at dispatch.

    THREE KNOBS THAT CANNOT BE LOOKED UP WHERE THEY ARE ENFORCED.
    `job_default_s`, `job_max_s` and `job_output_max` are read inside
    the sandbox container -- stdlib only, no database, no DSN -- so a
    settings table cannot reach them. Resolved for the story at dispatch
    they are literals, like every other value in this list.

    BUDGETS OPEN NOTHING, which is what lets them join a closed list
    whose rule is that nothing in it is a key. They are integers, and
    the worst a wrong one does is stop a job early.

    Empty for a production run: the reader in the sandbox falls back to
    the same numbers when the variable is absent, so the common case
    carries no env at all.
    """
    return [{"name": name, "value": value}
            for name, value in sorted((spec.timings or {}).items())]


def _workspace_token_env(spec: ExecutionSpec) -> list:
    """The worker's proof that a caller is the conductor. NEVER the sandbox.

    `podfs` binds 0.0.0.0:9101 in the worker container and the sandbox is
    a container in the SAME pod, so it shares the network namespace and
    the model's own `bash` reaches nine write operations on loopback --
    `commit`, `credentials`, `revoke_credentials` among them. See
    kernel/workspace/podfs_auth.py for why the token is derived from the
    execution id rather than stored.

    ONLY ON THE WORKER, and that is asserted from both sides. The sandbox
    container's env below stays the closed list it is, and
    `bridge/tools_core.py::_safe_env` -- a SECOND allowlist applied inside
    the sandbox before `bash` sees anything -- does not carry this name
    either. A secret kept out of one list and not the other is not kept
    out, which is how the proxy variables were "fixed" twice and still
    did not reach the model.

    `TokenMissing` PROPAGATES, deliberately. Without
    AGENT_TOKEN_SIGNING_KEY the conductor cannot derive the token it will
    later have to present, and the worker cannot be handed the one it
    checks -- so the pod would come up 2/2 and refuse every workspace
    operation, which reads as a hung execution rather than as an unset
    secret. Raising here names the variable at the moment of the
    decision, before a PVC and a pod exist to clean up. E0/Docker never
    builds a pod manifest at all, so the local substrate pays nothing.

    NO EXECUTION ID MEANS NO VARIABLE -- not a blank one, and not a
    shared one. `derive` is a pure function of the id, so `None` would
    hand every id-less pod the SAME token and one leak would open all of
    them; that is the bug this whole module is closing, so it is not on
    offer. Nor is it an error, because a manifest is built without an id
    by things that never dispatch it: `ExecutionSpec.execution_id` is
    declared optional, `pod_manifest` already omits the `execution-id`
    label when it is absent, and `kubernetes.py` carries a whole branch
    for naming such a pod -- with the recorded finding that no
    production path reaches it, every caller of `spec_for_dispatch`
    passing an int off `executions.id`.

    So an id-less manifest is an INSPECTED manifest, and inspecting one
    must not require a production secret -- this module's own docstring
    is that the translation is "the part worth testing on a laptop". If
    such a pod were ever dispatched it still fails CLOSED and loudly:
    `podfs.serve()` reads its token at startup, `configured_token()`
    raises on an absent variable, and the worker dies before serving a
    call -- with its own message naming AGENTOS_PODFS_TOKEN, which
    `terminationMessagePolicy: FallbackToLogsOnError` lifts into the pod
    status that `await_ready` already raises.
    """
    if spec.execution_id is None:
        return []
    return [{"name": podfs_auth.ENV_VAR,
             "value": podfs_auth.derive(spec.execution_id)}]


def _security_context(spec: ExecutionSpec, *, sandbox: bool) -> dict:
    """No privilege, and a uid that differs between the two containers.

    The Verifier already runs as a different uid from the executor
    deliberately — so nothing the executor left owned and writable can be
    executed by the thing checking it. The same reasoning applies inside
    this pod: the sandbox must not be able to overwrite the worker's
    files in the shared workspace.

    THE WORKER NEEDS ONE OF ITS OWN, AND DID NOT HAVE IT. This set
    `runAsNonRoot: True` for both containers but only ever gave the
    SANDBOX a `runAsUser`; the worker was left to the image's own user.
    That was survivable while the worker ran a stock image nobody
    inspected and merely slept. It stopped being survivable the moment
    the worker ran the kernel's image to serve workspace operations —
    that image declares no USER, so kubelet refused the container by
    name:

        container has runAsNonRoot and image will run as root

    A pod stuck at 1/2 with the sandbox happily running, which reads as
    a sandbox problem and is not one.

    `spec.user` still wins where a contract declares one; WORKER_UID is
    the floor, not the policy.
    """
    ctx = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
    }
    if sandbox:
        # A distinct uid from the worker's. Not spec.user: that is the
        # executor's identity, and the sandbox is the thing it is being
        # protected from.
        ctx["runAsUser"] = SANDBOX_UID
    else:
        ctx["runAsUser"] = _worker_uid(spec)
    return ctx


def pod_manifest(spec: ExecutionSpec, name: str, *,
                 namespace: str, pvc_name: str,
                 service_account: str, bridge_image: str,
                 workspace_secret: str | None = None,
                 control_panel_url: str = "",
                 egress_proxy: str = "",
                 sandbox_image: str | None = None,
                 sql: k8s_sqlproxy.Connection | None = None,
                 labels: dict | None = None) -> dict:
    """The pod one execution runs in.

    `sandbox_image` defaults to the worker's: E0 runs the same image for
    both today, and a second image is a deployment choice rather than a
    property of the translation.

    `sql` is D1: the pod's OWN database connection, so that when the leg
    moves here in D2 the advisory lock that guards it can move with it.
    None is E0, a laptop, and `next` before a person has created the key
    — the pod is then exactly what it was, with the leg still driven from
    the conductor. See kernel/drivers/k8s_sqlproxy.py for why the proxy
    carries a key rather than the pod's identity, and why its listener is
    a socket on a volume the sandbox does not mount.
    """
    # `agentos.io/egress` is what the NetworkPolicy selects on, NOT the
    # app name. The namespace is default-deny, and the policy that opens
    # a path to the proxy has to cover execution pods AND verifier Jobs
    # — which carry different app labels for good reasons. Selecting on
    # intent means adding a third kind of pod cannot silently leave it
    # with no egress, which under default-deny is a workload that starts
    # and then fails on its first outbound call.
    #
    # `agentos.io/kind` is a SECOND label answering a second question,
    # and the two are not interchangeable. `agentos.io/egress` says what
    # this pod may reach; `agentos.io/kind` says that it is work, which
    # is what the anti-affinity rule in k8s_placement selects on to keep
    # one story to a machine. A pod could plausibly have one and not the
    # other -- the warm placeholders have neither, deliberately.
    meta_labels = {"app": "agentos-execution", "managed-by": "kernel",
                   "agentos.io/egress": "proxy",
                   **k8s_placement.kind_label(k8s_placement.EXECUTION),
                   **(labels or {})}
    if spec.execution_id is not None:
        meta_labels["execution-id"] = str(spec.execution_id)

    volumes = [{"name": "workspace",
                "persistentVolumeClaim": {"claimName": pvc_name}},
               k8s_bridge.volume(), k8s_workspace.runtime_volume()]
    # The proxy's key, which NO OTHER CONTAINER IN THIS POD MOUNTS. A
    # volume in `volumes` is reachable only by a container that names it
    # in `volumeMounts`, and that is the whole per-container boundary
    # Kubernetes offers — `serviceAccountName` is a POD field, so
    # identity separation is not on the table.
    volumes += k8s_sqlproxy.volumes(sql)
    mount = [{"name": "workspace", "mountPath": WORKSPACE_PATH}]

    pod: dict = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": meta_labels},
        "spec": {
            "restartPolicy": "Never",
            "serviceAccountName": service_account,
            # SEPARATE PID NAMESPACES, STATED. False is the default, and
            # this design depends on it: sharing would let the sandbox
            # read /proc/<worker>/environ and take the DSN, at which
            # point no other boundary in this pod means anything. A
            # default relied on silently is a default somebody changes.
            "shareProcessNamespace": False,
            # BOTH CONTAINERS WRITE THE SAME TREE, as different uids.
            # The model edits files through the sandbox; the worker
            # commits them and reads them for artifacts. Without a
            # shared group the claim is owned by whoever touched it
            # first and the other gets EACCES on a workspace it half
            # owns -- which surfaces as the model failing at its first
            # edit, or a commit that finds nothing staged.
            #
            # fsGroup also chowns the claim on mount, so the tree the
            # init container composes is writable by both without
            # anything running as root to arrange it.
            "securityContext": {"fsGroup": WORKSPACE_GID},
            # The grace period is 15 seconds on Spot, best effort, and a
            # larger number is silently truncated rather than honoured.
            # Asking for what we get keeps the manifest honest.
            "terminationGracePeriodSeconds": 15,
            "volumes": volumes,
            # DELIVERED BEFORE ANYTHING RUNS, so a pod whose bridge could
            # not be copied never becomes Ready -- rather than starting a
            # sandbox that answers nothing and failing later, as a tool
            # call that times out and reads as a hung model.
            # ORDERED. Init containers run in sequence: the shim is
            # delivered, then the workspace is composed, then the
            # containers start. A failure in either leaves the pod short
            # of Ready rather than running against something half-built.
            # FIRST, AND STILL ORDERED. The Cloud SQL proxy is a NATIVE
            # sidecar -- an init container with `restartPolicy: Always` --
            # so the sequence above is unchanged: a native sidecar only
            # has to START before the next one runs, and putting it at the
            # head means the socket exists before anything wants it and
            # outlives the worker on teardown. k8s_sqlproxy says why that
            # ordering is the reason to spell it this way.
            "initContainers": k8s_sqlproxy.sidecars(
                sql, uid=_worker_uid(spec),
                proxy_env=_proxy_env(egress_proxy)) + _init_containers(
                spec, bridge_image, mount[0],
                workspace_secret, control_panel_url, egress_proxy),
            "containers": [
                {
                    "name": WORKER,
                    # THE KERNEL'S IMAGE, not the contract's. This
                    # container serves workspace operations -- commit,
                    # archive, the explorer -- and those are kernel
                    # code; the contract's image is the environment the
                    # WORK runs in and carries none of it. The sandbox
                    # keeps the contract's image, which is where it
                    # matters.
                    "image": bridge_image,
                    # IT USED TO SLEEP, and so the conductor had no way
                    # to reach a tree it does not share a filesystem
                    # with -- story_commit, emit_artifact, git archive
                    # and the explorer all read a local path.
                    "command": k8s_workspace.worker_command(),
                    # B4a -- AND THE WORKSPACE TOKEN, which only this
                    # container may hold. `_workspace_token_env` says
                    # why it is here, why it is not below, and why a
                    # conductor without the signing key refuses to build
                    # a pod at all.
                    # AND THE WAY OUT, which this container did not have.
                    #
                    # The init container was given the proxy -- which is
                    # why the CLONE worked -- and the sandbox was given
                    # it, and the worker was given neither. But the
                    # worker is where `story_commit` runs, and
                    # `story_commit` PUSHES. So a story could do all of
                    # its work and then fail to land it.
                    #
                    # Measured in exec-197: `git ls-remote origin` from
                    # this container took 133 SECONDS and then
                    # "Failed to connect to github.com port 443".
                    # Default-deny drops rather than refusing, so it is
                    # a hang followed by a timeout -- the same shape as
                    # the pip install that cost twenty minutes, on the
                    # last step of a story instead of the first.
                    #
                    # The FULL proxy, and the pod's label already agrees:
                    # `agentos.io/egress: proxy` opens 3128, whose
                    # allowlist carries the forge. See k8s_egress.py for
                    # why the label and the port are one decision.
                    # AND D1'S DATABASE, WHICH IS ALSO WORKER-ONLY. A
                    # socket path and a `secretKeyRef` password, for the
                    # container that will hold the leg's advisory lock.
                    # The sandbox's env below stays the closed list it
                    # is, and `shareProcessNamespace: False` above is
                    # what stops it reading these out of /proc.
                    "env": [{"name": k, "value": v}
                            for k, v in sorted(spec.env.items())]
                           + _workspace_token_env(spec)
                           + _panel_env(control_panel_url)
                           + k8s_sqlproxy.worker_env(sql)
                           # WHICH EXECUTION THIS POD IS. The leg
                           # loop refuses to start without it and
                           # will not guess -- a default would be
                           # an execution id nobody chose, and the
                           # worker would drive somebody else's
                           # story. Set on the clone container
                           # since E7e; the worker needs it too now
                           # that it runs the legs.
                           + [{"name": "AGENTOS_EXECUTION_ID",
                               "value": str(spec.execution_id)}]
                           # D2-finish: I10's other half. The leg
                           # runs HERE once the cutover is on, so
                           # the model key is here -- as a secret
                           # REFERENCE, never a plain value, and
                           # never on the sandbox's closed list
                           # below. See `model_key_env`.
                           + model_key_env(workspace_secret)
                           + _proxy_env(egress_proxy),
                    "resources": _resources(spec, sandbox=False),
                    # THE RUNTIME DIR, AND ONLY HERE. `GitDriver`
                    # persists `credential.helper` pointing at a path
                    # under it, so every later fetch and push resolves
                    # the broker bearer from this volume. The sandbox
                    # does not mount it -- that is the whole point of
                    # git_bearer.py keeping the bearer out of the tree.
                    "volumeMounts": mount + [k8s_workspace.runtime_mount()],
                    "securityContext": _security_context(spec, sandbox=False),
                },
                {
                    "name": SANDBOX,
                    "image": sandbox_image or spec.image,
                    # IT USED TO SLEEP. The pod came up, both containers
                    # idled, and nothing served the bridge -- so every
                    # tool call had nowhere to land even once the
                    # conductor could address the pod. See
                    # kernel/drivers/k8s_bridge.py for how the shim
                    # reaches an image that does not contain it.
                    "command": k8s_bridge.sandbox_command(),
                    # NO CREDENTIAL, AND EXACTLY ONE PATH.
                    #
                    # This was `[]` with a comment reading "Not 'no
                    # secrets' — none at all", and the absolutism cost a
                    # run. `kernel/bridge/tools_core.py` resolves the
                    # workspace root from WORKSPACE and falls back to
                    # `/tmp` when it is unset or not a directory. So the
                    # shim in an env-less sandbox rooted every relative
                    # path, and `bash`'s cwd, at `/tmp` -- an empty
                    # directory on a pod whose `/workspace` held a
                    # correct checkout the whole time. Execution 10 ran
                    # `list_dir` twelve times, found nothing, and failed
                    # for want of a repository that was right there.
                    #
                    # A MOUNT PATH IS NOT A CREDENTIAL. The property
                    # that matters is that nothing here opens anything:
                    # no token, no DSN, no bearer, no bucket. `/workspace`
                    # is a fact about this container's own filesystem,
                    # already visible to anything running in it as `ls
                    # /`, and withholding it did not make the sandbox
                    # safer -- it made the sandbox wrong.
                    #
                    # `assert_no_credentials` still governs what may go
                    # in `spec.env`; this is the manifest builder, and
                    # the list stays a closed one, reviewed here.
                    "env": [
                        {"name": "WORKSPACE", "value": WORKSPACE_PATH},
                        # A WRITABLE HOME, because the sandbox has a uid
                        # and no home directory. `runAsUser: 65532` is an
                        # id the image has no passwd entry for, so HOME
                        # resolves to `/` -- which uid 65532 cannot write.
                        #
                        # What that costs is the one install path the
                        # Verifier can reproduce. `env_install(pip, ...)`
                        # runs a bare `pip install`; system site-packages
                        # is root-owned, so pip falls back to the user
                        # site and dies:
                        #
                        #   OSError: [Errno 13] Permission denied: '/.local'
                        #
                        # A permission error naming a path nobody chose,
                        # from the tool whose whole description is "use
                        # this instead of running pip through the shell".
                        # `/tmp` is 1777 and on the container's own
                        # layer, so it is writable and it is not the
                        # workspace -- pip's cache does not belong in
                        # something `story_commit` walks.
                        {"name": "HOME", "value": "/tmp"},
                        # GIT WORKS IN A TREE IT DOES NOT OWN.
                        #
                        # The init container clones as the worker's uid;
                        # the sandbox runs as 65532. Git's ownership
                        # check then refuses every command in the
                        # checkout:
                        #
                        #   fatal: detected dubious ownership in
                        #   repository at '/workspace/repos/...'
                        #
                        # So `git status`, `git diff` and `git log` --
                        # the first things an executor runs to orient --
                        # all fail. It was invisible until now only
                        # because the old sandbox image had no `git` at
                        # all: "not found" instead of "refused".
                        #
                        # The env form rather than a gitconfig file: it
                        # cannot be missing, and it needs nothing
                        # writable to live in. `safe.directory` is git's
                        # own answer for a shared tree with more than one
                        # uid on it -- it relaxes an ownership check, not
                        # a permission; the sandbox still writes only
                        # what the mode bits allow.
                        {"name": "GIT_CONFIG_COUNT", "value": "1"},
                        {"name": "GIT_CONFIG_KEY_0",
                         "value": "safe.directory"},
                        {"name": "GIT_CONFIG_VALUE_0", "value": "*"},
                        # B3 -- AGENTOS_CREDENTIALS, appended rather
                        # than written: `_credentials_env` takes name
                        # and path from `workspace/credentials.py`, the
                        # one place that computes them and the same one
                        # the Docker sandbox reads. A pointer, not a
                        # secret, so the closed list stays closed.
                        # The timings profile's sandbox knobs, when
                        # this story is running under one. Absent on
                        # a production run.
                    ] + _credentials_env() + _proxy_env(egress_proxy)
                      + _timings_env(spec),
                    "resources": _resources(spec),
                    "volumeMounts": mount + [k8s_bridge.sandbox_mount()],
                    "securityContext": _security_context(spec, sandbox=True),
                },
            ],
        },
    }

    runtime = RUNTIME_CLASS.get(spec.isolation)
    if runtime:
        pod["spec"]["runtimeClassName"] = runtime
    # ALWAYS, INCLUDING THE DEFAULT. This used to skip "standard" on the
    # reasoning that the default needs no selector -- true on Autopilot,
    # where Google places the pod and there are no pools to choose
    # between. On Standard the unselected pod lands on whatever has room,
    # which is usually the platform pool, where it shares a machine with
    # the conductor and breaks one-story-per-node in the quietest
    # possible way: everything runs, just slower and on the wrong
    # hardware. The default is a real pool now, so it gets a real
    # selector.
    place_on_pool(pod["spec"], spec.node_class, namespace)
    # AND THE POOL IS ONLY HALF OF IT. The selector puts the pod among
    # the right machines; this keeps it alone on the one it lands on.
    # k8s_placement.py has the argument for why packing is the wrong
    # instinct for work that saturates what it is given.
    pod["spec"]["affinity"] = k8s_placement.one_per_node()
    _keep_the_reason(pod)
    return pod


def _panel_env(control_panel_url: str) -> list:
    """Where the worker sends an artifact it emitted.

    A PLAIN VALUE, NOT A SECRET, which is the only reason it may sit in
    a container env at all -- the same reasoning `_init_containers`
    already applies when it passes this to the composing container for
    the git credential helper.

    The worker needs it because `emit_artifact` runs in the leg, the leg
    runs in this pod since the leg-in-pod cutover, and this pod is
    denied the bucket on purpose. Without it the emit has nowhere
    durable to put the bytes and refuses -- correct, and useless. See
    `kernel/harness/emit.py::_store_durably`.
    """
    return ([{"name": "CONTROL_PANEL_URL", "value": control_panel_url}]
            if control_panel_url else [])


def _keep_the_reason(pod: dict) -> None:
    """Put a dead container's last output where the pod status keeps it.

    THE COST OF NOT DOING THIS, measured. `workspace-compose` exited 1
    on five consecutive dispatches. The cause was a single line in its
    log -- `could not read Username for 'https://github.com'` -- and
    nobody could read it, because dispatch destroys the pod when
    `await_ready` raises and `kubectl logs` on a deleted pod returns
    nothing. Finding it took a script that polled the API server in a
    loop trying to win a race against our own cleanup.

    Kubernetes will do this for us. The DEFAULT policy is `File`: the
    termination message is whatever the container wrote to
    /dev/termination-log, which for a Python process that died of an
    exception is nothing at all. `FallbackToLogsOnError` says "if the
    container failed and wrote no message, use the tail of its log" --
    so the reason lands in `status.containerStatuses[].state.terminated
    .message`, which is part of the pod object the conductor ALREADY
    reads, arrives in the exception `await_ready` already raises, and
    survives the deletion because it was copied out before it.

    Stamped over every container here rather than set in each builder:
    it is a property of how this kernel wants to be debugged, not of
    what any one container does, and a builder that forgot it would go
    unnoticed until the day that container was the one that died.
    """
    for key in ("initContainers", "containers"):
        for container in pod["spec"].get(key) or []:
            container["terminationMessagePolicy"] = "FallbackToLogsOnError"


def _init_containers(spec: ExecutionSpec, bridge_image: str,
                     workspace_mount: dict,
                     workspace_secret: str | None = None,
                     control_panel_url: str = "",
                     egress_proxy: str = "") -> list:
    """What must succeed before either container starts.

    The bridge always: a sandbox with no shim answers no tool call. The
    workspace only when there is something to compose — an errand with
    no grants has an empty workspace by design, and a container that
    clones nothing can still fail.
    """
    # The WORKER's uid, not the spec's-or-nothing. An init container
    # that composes as root leaves a tree neither container can write,
    # and one that composes as nobody-in-particular leaves one the
    # worker cannot commit.
    uid = _worker_uid(spec)
    containers = [k8s_bridge.init_container(bridge_image, uid)]
    if k8s_workspace.needed(spec):
        containers.append(k8s_workspace.init_container(
            spec, bridge_image, workspace_mount, uid,
            secret_name=workspace_secret,
            control_panel_url=control_panel_url,
            egress_proxy=egress_proxy))
        # P3 -- AFTER THE CLONE AND BEFORE THE SANDBOX. `env_setup`'s
        # first run leaves the leg: `LEG_MAX_S` is 2700s and a
        # thirty-minute install inside a leg leaves fifteen minutes
        # for the work (plan 12.5). Init containers run in order, so
        # this one sees the tree the line above composed.
        #
        # THE SANDBOX's UID, not `uid`. What it writes is EXECUTED by
        # the sandbox, and the two-uid boundary means the worker's
        # files are not the sandbox's to run. The difference from the
        # line above is the boundary working, not an inconsistency.
        containers.append(k8s_workspace.setup_container(
            spec, workspace_mount, SANDBOX_UID,
            egress_proxy=egress_proxy))
    return containers


def pvc_manifest(spec: ExecutionSpec, name: str, *, namespace: str) -> dict:
    """The workspace claim.

    ReadWriteOncePod, not ReadWriteOnce. RWO permits two pods on the SAME
    NODE to mount read-write, and for a git working tree where a
    replacement pod may be scheduled while the old one is still dying,
    the API server should refuse the second binding rather than let two
    writers race the index.
    """
    gib = spec.volume_gib or 50
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"managed-by": "kernel"}},
        "spec": {
            "accessModes": ["ReadWriteOncePod"],
            "storageClassName": STORAGE_CLASS.get(spec.volume_class,
                                                  STORAGE_CLASS["standard"]),
            "resources": {"requests": {"storage": f"{gib}Gi"}},
        },
    }


# ── D2-finish: the model key, in the worker and nowhere else ───────────
#
# I10 is the invariant: "the model key is in the WORKER and `bash` still
# cannot read it". D2 could state its premise and not its proof, because
# the key was not in the pod at all -- `POD_ENV_ALLOWLIST` is a closed
# list and carries none. This is the half that puts it there.
#
# A SECRET REFERENCE, NEVER A PLAIN VALUE, and the allowlist route would
# have been the wrong one. `POD_ENV_ALLOWLIST` copies the conductor's
# environment into `spec.env`, and `spec.env` is rendered as literal
# `{"name": ..., "value": ...}` pairs in the manifest -- so a key routed
# that way is readable in `kubectl get pod -o yaml` by anybody with pod
# read on the namespace, which after the RBAC split is still three
# ServiceAccounts. `credential_env` already had the right shape for the
# clone container's token and this follows it exactly.
#
# ON THE WORKER CONTAINER ONLY. The sandbox's env is a CLOSED LITERAL
# LIST a few hundred lines above -- it does not inherit `spec.env` and
# cannot inherit this. That is what makes I10 provable rather than
# asserted: the boundary is a list somebody has to edit, not a filter
# somebody has to maintain.
MODEL_KEYS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")


def model_key_env(secret_name: str | None) -> list:
    """The model key the leg needs, as secret references.

    `optional: True` on every one, deliberately. A deployment configures
    the vendor it uses; requiring both would make a pod with only an
    Anthropic key fail to start, and a pod that will not start is a
    story that cannot report why. The leg's own adapter says which key
    it needs and fails with a sentence when it is absent --
    `harness/models/config.py` maps vendor to variable.

    None means no secret, which is E0 and the corpus: the leg runs in
    the conductor there and reads the key from its own environment.
    """
    if not secret_name:
        return []
    return [{"name": key,
             "valueFrom": {"secretKeyRef": {"name": secret_name,
                                            "key": key,
                                            "optional": True}}}
            for key in MODEL_KEYS]
