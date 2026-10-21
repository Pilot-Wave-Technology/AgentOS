"""A run whose OUTPUT IS A DIRECTORY, as a Job. Manifest only.

The environment replay installs the executor's recorded packages once,
in a networked container, and carries the result to criteria that have
no network. On Docker the carrying is `GET /containers/{id}/archive`
before the container is destroyed; a Job's pod cannot be read after it
exits, so the directory has to leave under its own power.

TWO CONTAINERS, ONE POD, ONE SHARED VOLUME:

    work      the CONTRACT's image, running the install or the setup
              script. Its command is wrapped so that whatever happens
              it writes its exit status to a signal file.
    capture   OUR image, waiting for that file, then tarring the
              directory into the artifact store and printing the id.

The split is not stylistic. The work container is an image a contract
named: it has no kernel/, no cloud SDK, and no business acquiring
either. Everything the capture needs lives in our image.

WHY A FILE AND NOT A PROCESS. `shareProcessNamespace` is False in every
pod this kernel builds — sharing it would let one container read
another's /proc/<pid>/environ — so the two containers have nothing in
common but the volume, and the volume is how they talk.
"""

from __future__ import annotations

from kernel.drivers.k8s_job import (TTL_AFTER_FINISHED_S, fetch_init,
                                    fetch_volumes)
from kernel.spec import ExecutionSpec

WORK = "work"
CAPTURE = "capture"

SIGNAL_DIR = "/agentos-signal"
SIGNAL_FILE = f"{SIGNAL_DIR}/exit"

# Beyond the work container's own budget, how long the capture sidecar
# keeps waiting before it gives up and says so. Generous: the Job's
# activeDeadlineSeconds is the real bound and this is the backstop for a
# work container that dies without its shell running the trap.
CAPTURE_GRACE_S = 120

# `"$0" "$@"` re-runs the ORIGINAL argv exactly, whatever shape it had —
# `sh -c '...'` and a plain `["pytest", "-q"]` both survive it, because
# sh assigns $0 and $@ from the words after the script. Building a new
# command line by joining and quoting would change what an approved
# setup script means.
_WRAPPER = (
    'set +e\n'
    '"$0" "$@"\n'
    '__rc=$?\n'
    f'echo "$__rc" > {SIGNAL_FILE}\n'
    'exit $__rc\n'
)


def wrapped_command(command: list) -> list:
    """The caller's command, plus a signal when it stops.

    WRITTEN WHATEVER HAPPENS — `set +e` and an unconditional write — so
    a failing setup script still releases the sidecar. Without that the
    capture container waits out its whole grace period on every failure,
    and a broken setup costs two minutes instead of two seconds.
    """
    return ["sh", "-c", _WRAPPER, *(command or ["true"])]


def _covered(path: str, dests: list) -> bool:
    """Is `path` already inside a volume somebody else is mounting?

    The prepared-mount case fetches into /tmp and captures /tmp/verify.
    Mounting a fresh emptyDir at /tmp/verify would shadow the tree that
    was just unpacked there, and the capture would come back empty —
    which reads downstream as a setup script that produced nothing.
    """
    return any(path == d or path.startswith(d.rstrip("/") + "/")
               for d in dests)


def capture_manifest(spec: ExecutionSpec, name: str, *, namespace: str,
                     timeout_s: float, service_account: str,
                     capture_dir: str, capture_image: str,
                     capture_env: dict | None = None,
                     fetch: list | None = None, fetch_image: str = "",
                     fetch_env: dict | None = None,
                     labels: dict | None = None) -> dict:
    """One capture run, as a Job."""
    deadline = max(1, int(timeout_s))
    # `proxy`, NOT `store`, and it is not both. This pod reaches the
    # artifact store to PUT its result, and its work container also needs
    # a package index and a git host — it is the one container in the
    # whole Verifier that is networked on purpose. `proxy` covers both:
    # squid's `full_hosts` includes .googleapis.com.
    #
    # THE LABEL IS SINGLE-VALUED. An earlier comment here read "it
    # carries `proxy` as well, and the broader of the two is what the pod
    # gets" — true of squid's allowlist, FALSE of the NetworkPolicy,
    # which selects on one value. `proxy` replaces `store`; it does not
    # add to it, and 3129 is then closed to this pod.
    #
    # THE LABEL AND THE PROXY ARE ONE DECISION, so both come from
    # k8s_egress -- `fetch_env(VIA_CAPTURE)` in k8s_ephemeral is the
    # other half. Spelled apart, they disagreed: this pod was labelled
    # for 3128 and handed the 3129 address, which a default-deny policy
    # turns into a hang rather than a refusal.
    #
    # `capture` AND NOT `proxy`, since execution 205. The two share a
    # port and an allowlist; they differ in one rule, the metadata
    # server, which this pod's sidecar needs for `storage.Client()` and
    # which `proxy` must never grant -- it also selects the execution
    # pod, where the sandbox shares the network namespace. See the note
    # at VIA_CAPTURE.
    from kernel.drivers.k8s_egress import LABEL, VIA_CAPTURE  # noqa: PLC0415

    meta_labels = {"app": "agentos-capture", "managed-by": "kernel",
                   "agentos.ephemeral": "1",
                   LABEL: VIA_CAPTURE, **(labels or {})}
    if spec.execution_id is not None:
        meta_labels["execution-id"] = str(spec.execution_id)

    volumes, mounts = fetch_volumes(fetch) if fetch else ([], [])
    if not _covered(capture_dir, [d for d, _ in (fetch or [])]):
        volumes.append({"name": "captured", "emptyDir": {}})
        mounts.append({"name": "captured", "mountPath": capture_dir})
    volumes.append({"name": "signal", "emptyDir": {}})
    signal_mount = {"name": "signal", "mountPath": SIGNAL_DIR}

    work = {
        "name": WORK,
        "image": spec.image,
        "command": wrapped_command(spec.command),
        "env": [{"name": k, "value": v} for k, v in sorted(spec.env.items())],
        "resources": {
            "requests": {"cpu": str(spec.cpus),
                         "memory": f"{spec.memory_mb}Mi"},
            "limits": {"cpu": str(spec.cpus),
                       "memory": f"{spec.memory_mb}Mi"},
        },
        "volumeMounts": [*mounts, signal_mount],
    }

    sidecar = {
        "name": CAPTURE,
        "image": capture_image,
        "command": ["python", "-m", "kernel.artifacts.capture",
                    "--dir", capture_dir, "--signal", SIGNAL_FILE,
                    "--timeout", str(deadline + CAPTURE_GRACE_S)],
        "env": [{"name": k, "value": v}
                for k, v in sorted((capture_env or {}).items())],
        "resources": {
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": {"cpu": "500m", "memory": "1Gi"},
        },
        "volumeMounts": [*mounts, signal_mount],
        # ROOT, AND ONLY TO READ. The work container runs as whatever
        # its image declares, which for a setup script that installs
        # system packages is generally root — so files land root-owned
        # and a non-root sidecar would tar an unreadable directory and
        # report an empty environment. It executes nothing it finds
        # there; it tars it. Privilege escalation and every capability
        # are still dropped.
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsUser": 0,
        },
    }

    pod_spec: dict = {
        "restartPolicy": "Never",
        "serviceAccountName": service_account,
        "shareProcessNamespace": False,
        "volumes": volumes,
        "containers": [work, sidecar],
    }
    if fetch:
        pod_spec["initContainers"] = [
            fetch_init(spec, fetch, image=fetch_image or capture_image,
                       env=fetch_env or {}, mounts=mounts)]

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


def artifact_id_from(log: str) -> str | None:
    """The id the sidecar printed, or None.

    A content-addressed store means the caller cannot know the id in
    advance — it IS the hash of bytes that did not exist when the Job
    was created — so the log is the return channel. The LAST marker
    wins, because a retried sidecar would print more than one.
    """
    from kernel.artifacts.capture import MARKER

    # BELT AND BRACES, after executions 207 AND 208. `_logs` normalises,
    # which is the real fix and the right place for it -- but this
    # function is the single return channel of the whole capture
    # mechanism, and when it silently answers None a capture that WORKED
    # is reported as "stored nothing". It cost two deploy cycles to learn
    # that twice, so it is worth one shared call here.
    #
    # `as_text` and not a local decode: 208 was a `str` CONTAINING a
    # bytes repr, which a plain isinstance check does not catch, and a
    # second spelling of this logic is how the two rounds happened.
    from kernel.drivers.k8s_run import as_text            # noqa: PLC0415

    log = as_text(log) if log is not None else None

    found = None
    for line in (log or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == MARKER:
            found = parts[1]
    return found
