"""The execution pod's own database, out of the model's reach.

WHY A POD NEEDS ONE AT ALL. Phase D moves the model leg into the
execution pod, and `leg_lock` (kernel/worker/lease.py) is a Postgres
ADVISORY lock held for the whole leg — chosen precisely BECAUSE it dies
with its connection, so a worker that crashes cannot leave a run locked.
Move the leg without the lock and a conductor deploy releases it while
the pod's leg runs on: the exact double-run `lease.py` exists to prevent,
measured there as two models calling tools against one workspace. The
argument that module makes against a table lease — pick a timeout,
heartbeat, decide what a missed one means, and every choice is a way to
wedge or to double-run — applies here unchanged. So the lock has to be
held on a connection the POD owns, and that is what this module builds.

D1 BUILT THE CONNECTION AND NOTHING USED IT. D2 USES IT: `leg_runner`
takes `leg_lock` on the pod's own pooled connection and runs the leg
there, and this module's `from_env()` is what says whether a given
deployment does that at all. See `OFF` below — it is the switch, not a
placeholder waiting to be deleted.

KUBERNETES CANNOT GIVE TWO CONTAINERS IN ONE POD DIFFERENT IDENTITIES.
`serviceAccountName` is a POD field, and GKE Workload Identity resolves
by POD IP, which every container shares. So the obvious shape — a proxy
sidecar authenticating as the pod, which is what the conductor's own
sidecar does — is not available here. It needs egress to
169.254.169.254; NetworkPolicy is per-POD; and the sandbox in this pod
runs model-authored code. That grant is the one `verifier-egress-to-store`
in deploy/k8s/conductor.yaml refuses by name, because it would let the
model mint a Workload Identity token for `agentos-execution`. Adding
metadata to this pod is not on offer.

So the proxy carries a credential of its own, and four things are what
make a static key acceptable:

 1. A DEDICATED GSA holding `roles/cloudsql.client` AND NOTHING ELSE. A
    leaked key then buys "may open a TCP connection to the instance",
    not an identity.
 2. THE KEY IS MOUNTED INTO THE PROXY CONTAINER ONLY. Containers cannot
    read each other's mounts, and `agentos-execution` can neither `get`
    nor `list` Secrets and holds zero role bindings — so the Kubernetes
    API is not a way round the mount either.
 3. THE LISTENER IS A UNIX SOCKET ON THE WORKER-ONLY VOLUME, NEVER TCP.
    This is the load-bearing part. Containers in a pod SHARE A NETWORK
    NAMESPACE, so `--port=5432` — correct in the conductor's pod, which
    has no sandbox in it — would be reachable from the model's own
    `bash` on loopback, and the whole boundary would be decoration. A
    filesystem socket is not reachable, because the sandbox does not
    mount `/run/agentos`. That volume already keeps the git broker
    bearer away from the model (see `k8s_workspace.runtime_mount`), so
    this extends a boundary that exists rather than inventing one.
 4. A NARROW POSTGRES ROLE, its password worker-only as well: advisory
    locks, its own execution row, `step_journal`. Not the ledger, not
    the credentials tables. And never `--auto-iam-authn`, which on a TCP
    listener makes reaching the port sufficient to be authenticated.

WHAT A COMPROMISED SANDBOX WOULD HAVE TO DO: escape its container
filesystem to reach either the socket or the password — two separate
volumes it does not mount — and the cloud identity is never in its pod's
reach at all.

THE HONEST COST IS ROTATION. A static key does not expire, so it needs a
rotation story Workload Identity would have given free. That is the price
of Kubernetes having no per-container identity, and it is bounded by the
GSA holding exactly one role.

Decided in docs/refactor/conductor_revamp_plan.md §10, which also records
what was rejected: the conductor keeping the lock (it reintroduces the
conductor caring about a leg's liveness, which is what D removes), table
leases (they need the reaper the advisory lock does not), and the proxy
as its own Deployment (a network hop in front of the lock, and an
execution pod that is no longer self-contained).
"""

from __future__ import annotations

import os
from typing import NamedTuple

from kernel.drivers import k8s_workspace

# THE SAME IMAGE AND TAG AS THE CONDUCTOR'S SIDECAR in
# deploy/k8s/conductor.yaml. One proxy version in the cluster: a pod that
# reached the instance and a conductor that did not, differing only by a
# tag nobody compared, is not a debuggable state.
IMAGE = "gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.14.1"
CONTAINER = "cloud-sql-proxy"

KEY_VOLUME = "sqlproxy-key"
KEY_DIR = "/etc/agentos-sqlproxy"
KEY_ITEM = "key.json"

# Namespace Secret names. Defaults rather than required settings: they
# are facts about the deploy, and the deploy that creates them is the one
# that would have to name them twice otherwise.
KEY_SECRET = "execution-sqlproxy-key"
DB_SECRET = "execution-db"

INSTANCE_ENV = "AGENTOS_EXECUTION_SQL_INSTANCE"
KEY_SECRET_ENV = "AGENTOS_EXECUTION_SQL_KEY_SECRET"
DB_SECRET_ENV = "AGENTOS_EXECUTION_DB_SECRET"

# NOT CONFIGURED IS SPELT WITH A WORD, not with a blank. `render.py`
# refuses an empty ${...} on purpose — an empty value is indistinguishable
# from the rendering bug it exists to catch — so "this pod has no database
# yet" has to be a value a manifest can carry.
#
# THIS LINE USED TO END "D2 removes the need for it", AND D2 DECIDED THE
# OPPOSITE. Plan 11.5a prerequisite 3 already said `off` was load-bearing
# — build f999acc9 died because a missing secret aborted the deploy
# instead of falling back — and the two sentences were not in conflict
# about any fact, only about intent: one expected the pod's database to
# become universal, the other knew it could not be.
#
# It cannot, and the reason is not the secrets. There are deployments
# with no Cloud SQL instance to point at and no Kubernetes to run a
# sidecar in: E0, a laptop, every test in this suite, and any substrate
# where `SANDBOX_MODEL` is Docker and the workspace is this process's own
# filesystem. In all of them the leg runs where it has always run, in the
# conductor, and it is correct there.
#
# So `off` is PERMANENT, and D2 gives it a second job rather than
# retiring it: it is the one place that answers "does an execution pod
# drive its own leg?". `kernel/worker/executor_pass.legs_run_in_the_pod`
# reads exactly this, through `from_env`, and the conductor stands its
# own leg claiming down when the answer is yes. One variable, two
# consequences that must never disagree — which is why the conductor
# asks this module rather than carrying a switch of its own.
OFF = "off"

# A PATH IS NOT A CREDENTIAL, which is the only reason this may sit in a
# container's env at all. Same argument `k8s_spec._credentials_env` makes
# for AGENTOS_CREDENTIALS: the thing that opens the connection is the
# password below, delivered by Kubernetes into the worker and nowhere else.
HOST_ENV = "AGENTOS_LEG_DB_HOST"

# Worker-only, by `secretKeyRef` so no value is in the manifest, in the
# spec, or in a `kubectl describe`. `optional` for the reason
# `k8s_workspace.credential_env` gives: a cluster that has not been given
# the Secret yet should fail where the variable is READ, naming it, rather
# than hold every pod in ContainerCreating with an event nobody is watching.
DB_KEYS = (("AGENTOS_LEG_DB_USER", "username"),
           ("AGENTOS_LEG_DB_PASSWORD", "password"),
           ("AGENTOS_LEG_DB_NAME", "dbname"))

# The conductor's proven numbers, copied rather than trimmed. A proxy
# holding one connection surely needs less, and "surely" is how an OOMKill
# arrives in the middle of a leg that has already been paid for; measuring
# it is a change of its own.
CPUS = "250m"
MEMORY_MB = 512


class Connection(NamedTuple):
    """Which instance, and which Secrets carry the way in."""

    instance: str
    key_secret: str
    db_secret: str


def _named(var: str, fallback: str) -> str:
    if var not in os.environ:
        return fallback
    return os.environ[var].strip() or fallback


def from_env() -> Connection | None:
    """The deployment's answer, or None when the pod gets no database.

    None is the E0, laptop and test shape, and it is also `next` before a
    person has created the key: the pod is then exactly what it is today,
    with the leg still driven from the conductor. Read per call and not at
    import, for the reason `k8s_egress.proxy_for` gives — and with the
    membership test rather than `environ.get(x, default)`, because a
    default nobody can see is a policy nobody reviewed.
    """
    if INSTANCE_ENV not in os.environ:
        return None
    instance = os.environ[INSTANCE_ENV].strip()
    if not instance or instance == OFF:
        return None
    return Connection(instance, _named(KEY_SECRET_ENV, KEY_SECRET),
                      _named(DB_SECRET_ENV, DB_SECRET))


def socket_host(instance: str) -> str:
    """What the worker passes as `host` — a directory, as libpq wants.

    The proxy creates `<dir>/<instance>/.s.PGSQL.5432` under `--unix-socket`,
    which is the same shape Cloud Run's `/cloudsql/<instance>` has, so the
    DSN the worker builds reads like every other one in this codebase.

    THE MOUNT ROOT AND NOT A SUBDIRECTORY OF IT. `--unix-socket` wants a
    directory that already exists, and the only one this container is sure
    of is the emptyDir kubelet mounted. A tidier `/run/agentos/sql` would
    depend on the proxy creating its own parent, which is a detail of
    somebody else's release notes.
    """
    return f"{k8s_workspace.RUNTIME_DIR}/{instance}"


def volumes(sql: Connection | None) -> list:
    """The key, as a volume no other container in the pod mounts."""
    if sql is None:
        return []
    # 0o440 AND NOT 0o400. With a pod `fsGroup`, kubelet gives secret
    # files to root:fsGroup — so owner-only would be unreadable by the
    # non-root uid this proxy runs as, and the container would die on its
    # own credential file.
    return [{"name": KEY_VOLUME,
             "secret": {"secretName": sql.key_secret, "defaultMode": 0o440,
                        "optional": True}}]


def sidecars(sql: Connection | None, *, uid: int, proxy_env: list) -> list:
    """The proxy, as a native sidecar, or nothing.

    AN initContainer WITH `restartPolicy: Always`, for the two reasons
    the conductor's own sidecar gives and which bite harder here: it is
    ready BEFORE the worker, so the first thing the worker does is not
    race a socket into existence; and it is terminated AFTER, so a leg
    finishing its transaction during teardown is not cut off by its own
    proxy exiting first. Init containers run in sequence and a native
    sidecar only has to START before the next one, so putting it first
    costs the clone nothing.

    THE PROXY GOES OUT THROUGH SQUID. It needs `sqladmin.googleapis.com`
    and an OAuth token endpoint to mint the ephemeral certificate, and
    `.googleapis.com` is on the full allowlist already. The DATA
    connection is a raw TLS dial to the instance on 3307, which no proxy
    can carry — `execution-egress-to-sql` in deploy/k8s/conductor.yaml is
    the rule that admits it, pinned to the instance's address.

    THE SAME UID AS THE WORKER, which is what makes the socket
    connectable: the proxy creates it, and the process that has to open
    it is the worker. `k8s_spec` computes that uid once, from `spec.user`
    or the floor, and passes it here rather than a second parser agreeing
    by coincidence.
    """
    if sql is None:
        return []
    return [{
        "name": CONTAINER,
        "image": IMAGE,
        "restartPolicy": "Always",
        # NO `--port` AND NO `--address`: a TCP listener in this pod is
        # reachable from the sandbox, which shares the network namespace.
        # NO `--auto-iam-authn` either — on a TCP listener it makes
        # reaching the port sufficient to be authenticated, which is the
        # one property this design must not have.
        "args": ["--structured-logs",
                 f"--unix-socket={k8s_workspace.RUNTIME_DIR}",
                 f"--credentials-file={KEY_DIR}/{KEY_ITEM}",
                 sql.instance],
        "env": list(proxy_env),
        "volumeMounts": [k8s_workspace.runtime_mount(),
                         {"name": KEY_VOLUME, "mountPath": KEY_DIR,
                          "readOnly": True}],
        "resources": {
            "requests": {"cpu": CPUS, "memory": f"{MEMORY_MB}Mi"},
            "limits": {"cpu": CPUS, "memory": f"{MEMORY_MB}Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": uid,
        },
    }]


def worker_env(sql: Connection | None) -> list:
    """Where the socket is, and who to be on it. THE WORKER ONLY.

    Not the sandbox, and the pod design is what enforces it rather than
    this list: `shareProcessNamespace` is False and stated in
    `k8s_spec.pod_manifest` precisely so the sandbox cannot read
    /proc/<worker>/environ and take the DSN. A second allowlist inside
    the sandbox — `bridge/tools_core.py::_safe_env` — does not carry
    these names either, which is the same two-sided rule the workspace
    token is kept under.
    """
    if sql is None:
        return []
    return [{"name": HOST_ENV, "value": socket_host(sql.instance)}] + [
        {"name": var,
         "valueFrom": {"secretKeyRef": {"name": sql.db_secret, "key": key,
                                        "optional": True}}}
        for var, key in DB_KEYS]


# ── D2's assembly: the DSN, built in the pod from its parts ────────────

class DsnIncomplete(RuntimeError):
    """The pod cannot build a database URL from what it was given."""


def leg_dsn(environ) -> str:
    """The leg's DSN, assembled in the pod. Never pre-built in the
    manifest, and that is the point.

    `worker_env` hands the pod four separate variables -- the socket
    host as a plain value, and username/password/dbname as SECRET
    REFERENCES. A `DATABASE_URL` written by the manifest builder would
    have to interpolate the password, turning a secretKeyRef into a
    literal in the pod spec readable by `kubectl get pod -o yaml`. That
    is the same mistake `model_key_env` exists to avoid, one layer down.

    So the parts travel separately and the pod joins them. The password
    never leaves the Secret until it is inside the container that uses
    it.

    RAISES RATHER THAN RETURNING "". A worker that started with half a
    DSN would fail on its first query, minutes later, with an asyncpg
    error naming nothing useful -- and `AGENTOS_EXECUTION_SQL_INSTANCE`
    being `off` is a SEPARATE and legitimate state the caller checks
    first. If the parts are meant to be here and are not, that is a
    deployment fault and it should stop the container.
    """
    from urllib.parse import quote                           # noqa: PLC0415

    missing = [name for name in (HOST_ENV, *(v for v, _ in DB_KEYS))
               if not (environ.get(name) or "").strip()]
    if missing:
        raise DsnIncomplete(
            f"cannot build the leg's DSN: {', '.join(missing)} unset. "
            f"The socket host comes from the manifest and the other "
            f"three from the `execution-db` Secret -- an absent one is "
            f"usually that Secret missing a key, which resolves to "
            f"nothing because the references are `optional: True`.")

    user, password, dbname = (environ[v] for v, _ in DB_KEYS)
    host = environ[HOST_ENV]
    # `quote` on BOTH credentials. A generated password containing `@`
    # or `/` would otherwise split the URL somewhere else entirely, and
    # the failure is an authentication error against a host nobody
    # named.
    #
    # AND THE SOCKET GOES IN `?host=`, NOT THE AUTHORITY. `socket_host`
    # returns a DIRECTORY -- the proxy makes `<dir>/<instance>/
    # .s.PGSQL.5432` under `--unix-socket`. Written as
    # `@{dir}/{dbname}` the parser reads an empty host and a path of
    # `/cloudsql/x/agentos`, so the connection goes to a TCP host that
    # is not there. The query-parameter form is what libpq and asyncpg
    # both take for a unix socket.
    return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@/{quote(dbname, safe='')}?host={quote(host, safe='')}")
