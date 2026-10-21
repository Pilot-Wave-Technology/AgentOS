"""The init container that fills the claim before anything runs.

P3/D1: on Kubernetes the workspace is a ReadWriteOncePod claim owned by
the execution's own pod, so the conductor cannot compose it — the access
mode exists so the API server refuses a second writer. Composition moves
into the pod, and this is the manifest half of it.
`kernel/workspace/podinit.py` is the code half, and it calls the SAME
`CompositeDriver.acquire` the conductor calls on Docker.

ORDERED AFTER THE BRIDGE AND BEFORE THE CONTAINERS. Init containers run
in sequence, so the shim is delivered, then the workspace is composed,
then worker and sandbox start — and a failure in either leaves the pod
short of Ready rather than running against something half-built. That
matters more than it sounds: `runner.py` records a live failure where a
transient 404 from the credential broker failed a clone, the run
dispatched anyway, and the executor wrote a ten-step plan to recreate
branches the engine had already cut.

IT RUNS THE KERNEL'S IMAGE, not the contract's. Composing needs
`kernel.workspace`, and the contract's image is the environment the WORK
runs in — it carries no kernel and should not have to.

AND IT HOLDS THE ONE CREDENTIAL THE SANDBOX MUST NEVER SEE. Cloning a
private repo needs the git broker bearer, so this container gets it and
`/run/agentos` is shared with the WORKER and not the sandbox.
`git_bearer.py` writes the bearer outside `/workspace` precisely so a
sandbox that can read its own tree cannot read the credential; here that
is a volume boundary rather than a path convention, which is stronger.
The worker needs the same mount because `GitDriver` persists
`credential.helper` pointing at that path, so every later fetch and push
resolves it.

AND SINCE D1, THE POD'S DATABASE SOCKET IS HERE TOO. The Cloud SQL proxy
listens on a Unix socket under `/run/agentos` rather than on a port
precisely because this volume has that boundary already: containers in a
pod share a network namespace, so `127.0.0.1:5432` would be reachable
from the sandbox, and a filesystem socket is not. So the list of things
this mount keeps from the model is now two, and adding the sandbox to it
would give away both at once. See kernel/drivers/k8s_sqlproxy.py.
"""

from __future__ import annotations

import json

from kernel.spec import ExecutionSpec
from kernel.workspace.podinit import MOUNTS_ENV, STORY_ENV

# Shared by the composing init container and the worker, never the
# sandbox. See the module docstring.
RUNTIME_VOLUME = "runtime"
RUNTIME_DIR = "/run/agentos"

# THE CLONE'S allocation, and it stays small on purpose: a clone is
# IO-bound, so more CPU buys it nothing. This is not the dependency
# install -- see `setup_resources` below, which is a different job with
# a different answer.
INIT_CPUS = "0.5"
INIT_MEMORY_MB = 2048

# THE DEPENDENCY INSTALL'S FLOOR, separate from the clone's since B3 put
# a Rust toolchain in the image. Two cores because a single-threaded
# build on half of one is how `setup_budget_s` gets spent on waiting;
# 4 GB because a C++ translation unit at 2 GB is an OOM in the one
# container that most needs not to have one. Still a FLOOR -- a story on
# a larger tier gets its own, which is the rule that has not changed.
SETUP_FLOOR_CPUS = "2"
SETUP_FLOOR_MEMORY_MB = 4096


def setup_resources(spec) -> dict:
    """What the DEPENDENCY INSTALL gets: the sandbox's own tier.

    IT USED TO TAKE THE CLONE'S 0.5 vCPU / 2 GB, fixed on every rung,
    and that was the wrong shape twice over.

    IT IS NOT IO-BOUND. `env_setup` runs the tree's committed manifest
    -- `pip install`, `npm ci`, and since the image carries a compiler,
    a source build for any dependency without a prebuilt wheel. At 0.5
    vCPU that is slow, and at 2 GB a single large C++ translation unit
    can exhaust the container outright: the compiler would OOM in the
    one place it is most needed, on the tier that was bought to avoid
    exactly that.

    AND ONE STORY PER NODE MAKES THE CAP PURE WASTE. The execution pod
    owns its machine -- anti-affinity on `agentos.io/kind` -- and an init
    container runs when no other container in the pod has started. So
    during the install the node is paid for and otherwise idle, and
    capping the install at half a core leaves the rest of it doing
    nothing while a single-threaded build crawls.

    THE TIER, NOT A NUMBER. Expressed as the sandbox's allocation rather
    than an absolute, so it is right under both cluster shapes and
    follows the tiers up by itself when `sizes.py` is retuned to request
    nearly all of a Standard node. And it never inflates the pod: the
    scheduler takes `max(max(init), sum(regular))`, and the sandbox's
    tier alone is always less than that same tier plus the worker and
    the sql proxy beside it.

    FLOORED AT THE SETUP FLOOR, not the clone's, and B3 is why the two
    parted. The image now carries `cargo`, because the dependencies that
    lag a new CPython minor are Rust-backed -- and a Rust or C++ source
    build is the one thing in this pod that is neither IO-bound nor
    small. A wave of them against 0.5 vCPU times out inside
    `setup_budget_s`, and grpcio's translation units are an OOM risk at
    2 GB. Both failures arrive as "the checks fail for a reason that is
    not the work", which is the misdiagnosis `setups.py` exists against.

    The clone keeps `INIT_CPUS`/`INIT_MEMORY_MB`: it copies bytes.
    """
    cpus = max(float(SETUP_FLOOR_CPUS), float(getattr(spec, "cpus", 0) or 0))
    memory = max(SETUP_FLOOR_MEMORY_MB,
                 int(getattr(spec, "memory_mb", 0) or 0))
    return {
        "requests": {"cpu": f"{cpus:g}", "memory": f"{memory}Mi"},
        "limits": {"cpu": f"{cpus:g}", "memory": f"{memory}Mi"},
    }

# Non-secret settings the composing container needs, taken from the
# spec's own (already credential-free) environment.
INHERITED = ("AGENTOS_WORKSPACE_ROOT", "ARTIFACT_STORE", "ARTIFACT_BUCKET",
             "ARTIFACT_PREFIX", "AGENTOS_STORE_PROXY",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")

# THE CLONE CREDENTIAL DOES NOT TRAVEL THROUGH THE SPEC.
#
# The first version of this read OPENCLAW_AUTH_TOKEN off `spec.env` and
# a test caught it empty, which was the right answer for the wrong
# reason: `assert_no_credentials` REFUSES any spec env key ending in
# _TOKEN, _KEY, _SECRET or _PASSWORD. The bearer was not missing by
# accident -- the guard exists so an execution spec can never carry one,
# and routing a credential through it would have made that guard a
# formality while leaving the value in every log and describe output
# that prints a pod spec.
#
# So Kubernetes delivers it: a `secretKeyRef` naming the same namespace
# secret the conductor mounts. The value is never in the manifest, never
# in the spec, and reaches exactly the one container that clones.
CREDENTIAL_KEYS = ("OPENCLAW_AUTH_TOKEN",)


def credential_env(secret_name: str | None) -> list:
    """Secret references for the container that clones private repos.

    None means no secret is configured, which is the corpus and E0
    shape: public or local sources only. Deliberately not an error --
    a workspace of public repos composes perfectly well without one.
    """
    if not secret_name:
        return []
    return [{"name": key,
             "valueFrom": {"secretKeyRef": {"name": secret_name,
                                            "key": key,
                                            "optional": True}}}
            for key in CREDENTIAL_KEYS]


def runtime_volume() -> dict:
    return {"name": RUNTIME_VOLUME, "emptyDir": {}}


def runtime_mount() -> dict:
    return {"name": RUNTIME_VOLUME, "mountPath": RUNTIME_DIR}


def serialise(mounts) -> str:
    """The mount list as the pod will read it back.

    Field-by-field rather than a dataclass dump: what crosses here is a
    contract between two processes, and a field silently added on one
    side and missing on the other is the kind of drift this codebase
    keeps finding. `podinit.mounts_from` is the other half.
    """
    return json.dumps([
        {"at": m.at, "binding": m.binding, "source": m.source,
         "ref": m.ref, "writable": bool(m.writable), "kind": m.kind,
         "resource_id": getattr(m, "resource_id", "") or ""}
        for m in mounts])


# THE ONLY WAY OUT OF AN EXECUTION POD.
#
# `workload-egress-via-proxy` permits egress to the egress-proxy on 3128
# and to DNS, and nothing else -- so a clone that does not USE the proxy
# reaches nothing, and fails as a network timeout rather than as a
# policy refusal. The variables were expected to arrive by inheritance
# from the conductor, and the conductor has never had them: it makes no
# outbound calls on a model's behalf and its own policy is `egress: {}`.
#
# 3128 and not 3129: the store port's allowlist is the object store
# alone, which is right for a criterion and would refuse the forge.
def proxy_env(proxy: str) -> list:
    if not proxy:
        return []
    # NO_PROXY keeps in-cluster traffic off it. Without this the worker's
    # own calls to the control panel and the conductor's calls back into
    # the pod would be bounced through squid, which has no reason to
    # carry them and no allowlist entry for a pod IP.
    # ONE DECLARATION. This list and k8s_spec's disagreed on `::1`, on
    # the cluster suffix and on the pod CIDR, and both omitted
    # 169.254.169.254 -- the metadata server, which IS the pod's
    # identity. See k8s_egress.NO_PROXY.
    from kernel.drivers.k8s_egress import proxy_env as _shared  # noqa: PLC0415

    return _shared(proxy)


def init_container(spec: ExecutionSpec, image: str, workspace_mount: dict,
                   uid: int | None = None, secret_name: str | None = None,
                   control_panel_url: str = "",
                   egress_proxy: str = "") -> dict:
    """Compose the workspace, or fail and keep the pod out of Ready."""
    env = [{"name": MOUNTS_ENV, "value": serialise(spec.workspace_mounts)},
           {"name": "AGENTOS_EXECUTION_ID",
            "value": str(spec.execution_id or "")},
           # THE GIT BROKER SCOPES ON THIS. Without it the credential
           # helper returns "" (see `_credential_helper`), the clone of
           # a private repo runs unauthenticated, and the pod dies in
           # this container asking for a username. The Docker path reads
           # it from the database at compose time; a pod has no
           # database, so it is carried.
           {"name": STORY_ENV, "value": spec.story_id or ""}]
    env += [{"name": key, "value": spec.env[key]}
            for key in INHERITED if key in spec.env]
    # Where the credential broker answers. Passed by the driver rather
    # than read here: the manifest builder is a pure function of its
    # arguments, and which control panel is deployed is a deployment
    # fact. Not a secret, so it travels as a plain value.
    if control_panel_url:
        env.append({"name": "CONTROL_PANEL_URL", "value": control_panel_url})
    env += proxy_env(egress_proxy)
    env += credential_env(secret_name)

    container: dict = {
        "name": "workspace-compose",
        "image": image,
        "command": ["python", "-m", "kernel.workspace.podinit"],
        "env": sorted(env, key=lambda e: e["name"]),
        "volumeMounts": [workspace_mount, runtime_mount()],
        "resources": {
            "requests": {"cpu": INIT_CPUS, "memory": f"{INIT_MEMORY_MB}Mi"},
            "limits": {"cpu": INIT_CPUS, "memory": f"{INIT_MEMORY_MB}Mi"},
        },
    }
    if uid is not None:
        # The worker and sandbox read this tree as their own uid. Cloning
        # as a different one leaves a workspace the executor cannot write,
        # which surfaces as the model failing at its first edit.
        container["securityContext"] = {"runAsUser": uid,
                                        "runAsNonRoot": uid != 0}
    return container


def needed(spec: ExecutionSpec) -> bool:
    """Whether there is anything to compose.

    An errand with no grants has an empty workspace by design — the
    `none` binding — and a container that clones nothing is a container
    that can still fail. Omitting it keeps a profile with no mounts on
    the shortest path to Ready.
    """
    return bool(spec.workspace_mounts)


def worker_command(bind: str = "0.0.0.0") -> list[str]:
    """What the worker container runs instead of sleeping.

    `0.0.0.0` for the same reason the shim binds widely: the conductor
    is a different pod and there is no loopback route to this one. The
    reach is restricted by `execution-bridge-ingress`, which admits both
    ports from the conductor, and 9101 alone from the control panel's
    VPC connector and from the verifier Deployment (Phase E1) -- 9100
    runs arbitrary bash, and only the process driving the sandbox has
    business there. Nothing else from anywhere.
    """
    from kernel.workspace.podfs import WORKER_PORT  # noqa: PLC0415

    assert WORKER_PORT  # the policy above names this port; keep them paired
    # BOTH JOBS, AND THIS IS D2'S CUTOVER. This ran podfs alone, and
    # `executor_pass.conductor_leg_slots()` returns ZERO the moment
    # `AGENTOS_EXECUTION_SQL_INSTANCE` leaves `off` -- so with podfs
    # alone the fleet would admit, dispatch, and never run a leg.
    # `pod_main` serves the workspace surface AND drives this
    # execution's legs, and takes the container down if either stops.
    return ["python", "-m", "kernel.worker.pod_main", "--bind", bind]


# ── P3: the first install, out of the leg's wall clock ─────────────────

def setup_needed(spec) -> bool:
    """Whether there is a tree to install anything into.

    The same question `needed` asks, and for the same reason: a mount
    list of nothing is an errand, and an init container that walks no
    mounts is a container that can still fail while doing nothing.
    """
    return bool(spec.workspace_mounts)


def setup_container(spec, workspace_mount: dict, uid: int | None,
                    egress_proxy: str = "") -> dict:
    """`env_setup`'s first run, as an init container. Plan P3, 12.5.

    THE CONTRACT'S IMAGE, never the bridge's. `spec.image` is what the
    work will be done in and what the verifier will check against, so it
    is what the environment must be built in -- `prepare_job.py` states
    the same rule first and calls it defence 1. A `.venv` built on a
    different interpreter is a `.venv` the sandbox cannot use, and a
    PATH shim left by the executor's image is how F4 stops meaning
    anything.

    AFTER THE CLONE. `init_container` above composes the workspace;
    Kubernetes runs init containers in order, so this one sees the tree.
    Putting it before would install nothing and report nothing wrong.

    THE SANDBOX'S UID, not the worker's. What this writes -- `.venv`,
    `node_modules` -- is executed by the SANDBOX, and the two-uid
    boundary means the worker's files are not the sandbox's to run. The
    clone container uses the worker's uid because the worker commits the
    tree; this one differs on purpose, and the difference is the
    boundary working rather than an inconsistency.

    NETWORKED, through the proxy. Every command in `setups.REGISTRY`
    fetches packages, and the namespace is default-deny: an install that
    does not use the proxy does not fail, it HANGS. `proxy_env` sets
    both cases because curl reads lowercase and a half-set pair is a
    tool that silently bypasses the proxy.

    NEVER FAILS THE POD -- `setup_script.script` exits 0 whatever
    happened, and the reason is in that module: an init container that
    exits non-zero is a story that can never start and can never be
    repaired by the model that could have fixed the manifest.
    """
    from kernel.workspace.setup_script import script     # noqa: PLC0415

    # ATTRIBUTE ACCESS, as `serialise` above uses. A workspace
    # mount is an object, not a dict, and `.get` on one raises at
    # manifest-build time -- which is a dispatch that fails rather
    # than a pod that misbehaves, but only because nothing catches
    # it here.
    mounts = [getattr(m, "at", "") or ""
              for m in (spec.workspace_mounts or [])]
    container: dict = {
        "name": "workspace-setup",
        "image": spec.image,
        # FROM THE MOUNT, not imported. `k8s_spec` imports this
        # module, so naming `k8s_spec.WORKSPACE_PATH` here would be
        # a cycle -- and the mount dict is the authority anyway: it
        # is what the container will actually see.
        "command": ["sh", "-c", script(
            mounts, workspace_mount["mountPath"])],
        # A WRITABLE HOME, THE THIRD ROOM. The sandbox gets `HOME=/tmp`
        # from `k8s_spec` and the Verifier's prepare container sets its
        # own; this container was left with neither, so HOME resolved to
        # `/` -- which its uid cannot write. Every tool that keeps a
        # cache under `~` then fails on a path nobody chose: pip at
        # `/.local`, npm at `/.npm`, uv at `/.cache/uv`. The install is
        # the one job in the pod that does most of that, and it was the
        # one container without somewhere to put it.
        #
        # `/tmp` FOR THE SAME REASON THE SANDBOX USES IT: 1777, on the
        # container's own layer, and not the workspace -- a cache does
        # not belong in something `story_commit` walks with `git add -A`.
        "env": sorted(proxy_env(egress_proxy) + [{"name": "HOME",
                                                  "value": "/tmp"}],
                      key=lambda e: e["name"]),
        "volumeMounts": [workspace_mount],
        "resources": setup_resources(spec),
        # The report is the evidence, and a container that died before
        # writing it should say so where `kubectl describe` shows it.
        "terminationMessagePolicy": "FallbackToLogsOnError",
    }
    if uid is not None:
        container["securityContext"] = {"runAsUser": uid,
                                        "runAsNonRoot": uid != 0}
    return container
