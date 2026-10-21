"""Run a Job to completion and report what it said.

Separated from the driver because kubernetes.py is the seam and this is
the loop, and separated from k8s_job.py because that builds manifests
and this talks to a cluster. The DECISIONS about what an ending means
live in k8s_outcome.py, which needs no cluster and is asserted there.

NEVER RAISES ON A NON-ZERO EXIT, which is the contract `run_ephemeral`
states and the reason the verifier can use it at all. A failing check is
the ordinary case and is DATA: the caller has to tell "the check ran and
said no" from "the check could not run", because R1 makes only the first
of those a refusal. Raising here would turn every failing test suite
into an infrastructure error.

AND THE CONVERSE, which this module got wrong until now. The dict it
returned had three keys — exit_code, output, timed_out — which can say
exactly two things: the command exited N, or the budget was blown. Every
OTHER way a Job can end was squeezed into the first of those as
`exit_code=1`, and `from_exit` reads a non-zero exit as `fail`: THE WORK
IS WRONG. So a Spot preemption refused a story. So did an image that
would not pull. A fourth key, `infra_error`, is what lets the substrate
say the third thing, and kernel/verifier/outcome.py routes it to
`unrunnable` — `error`, which R1 makes non-refusing.

AND IT CARRIES A KIND. `infra_error` is an `InfraError`: the same
sentence, plus which of plan 5.8's rows it is. Row 3 (the cluster ended
this run) is the only one that must not spend strikes, and it used to be
told from row 2 by the SHAPE OF THE PROSE. See
kernel/drivers/infra_error.py.
"""

from __future__ import annotations

import ast
import asyncio
import logging

from kernel.drivers.infra_error import UNKNOWN, UNOBSERVED, InfraError
from kernel.drivers.k8s_call import LOGS_S, call, quiet
from kernel.drivers.k8s_logref import logs_are_in_cloud_logging

from kernel.drivers.k8s_outcome import CRITERION, classify

logger = logging.getLogger(__name__)

# How often to ask whether the Job has finished. A criterion runs for
# minutes to hours, so a tight poll buys nothing and costs API quota.
POLL_S = 2.0

# Past the Job's own activeDeadlineSeconds, how long to keep waiting for
# Kubernetes to mark it failed. The deadline is enforced cluster-side;
# this is only so a stuck watch cannot hang a verification for ever.
DEADLINE_GRACE_S = 60


async def run_job(batch, core, manifest: dict, *, namespace: str,
                  timeout_s: float, container: str = CRITERION,
                  extra_logs: tuple = ()) -> dict:
    """Create, wait, read the log, delete. Returns run_ephemeral's dict.

    `{"exit_code": int | None, "output": str, "timed_out": bool,
      "infra_error": InfraError | None}`.

    `exit_code` is None exactly when `infra_error` is set. A caller that
    ignores `infra_error` therefore gets nothing usable rather than a
    fabricated 1 — which would read as a failing check and refuse the
    story. Loud beats wrong.

    `container` is the one whose exit status IS the result. A capture
    Job has two, and only the work container's answer means anything —
    the sidecar's exit says whether the tar reached the store.

    `extra_logs` names further containers to read before the Job is
    deleted, returned under `extra`. The capture sidecar's id comes back
    that way, because a content-addressed store means the caller cannot
    know it in advance.
    """
    name = manifest["metadata"]["name"]
    await call(batch.create_namespaced_job,
               namespace=namespace, body=manifest)

    result, failed = None, None
    deadline = asyncio.get_event_loop().time() + timeout_s + DEADLINE_GRACE_S
    try:
        while result is None and failed is None:
            if asyncio.get_event_loop().time() > deadline:
                # The cluster enforces activeDeadlineSeconds itself, so
                # reaching here means the WATCH is wrong, not the
                # criterion. The old code called this a timeout, and a
                # timeout against a declared budget is a `fail` — so a
                # broken poll loop refused a story. It is an infra
                # error, which is what "our machinery failed" means.
                result = _infra(
                    f"the Job did not report a result within "
                    f"{DEADLINE_GRACE_S}s of its own deadline; this is the "
                    f"watch failing, not the criterion",
                    # UNKNOWN: our machinery failed, so nothing here saw
                    # the ending at all -- not even enough to say the
                    # cluster took it.
                    UNKNOWN)
                break
            job = await call(batch.read_namespaced_job_status,
                             name=name, namespace=namespace)
            status = job.status
            if status.succeeded:
                result = {"exit_code": 0, "timed_out": False,
                          "infra_error": None}
            elif status.failed:
                failed = status
            else:
                await asyncio.sleep(POLL_S)

        # ONE READ OF THE POD, before the delete in the finally. It
        # answers both questions — how this ended, and what was said —
        # and after the delete it answers neither.
        pod = await _find_pod(core, name, namespace)
        if failed is not None:
            result = _ended(pod, name, failed, container, namespace)
        output = await _logs(core, pod, namespace, container, name)
        extra = {c: await _logs(core, pod, namespace, c, name)
                 for c in extra_logs}
    finally:
        # STILL IN A `finally`, and still bounded: a delete that hangs
        # here would hold the loop open on the way out of a criterion
        # that has already finished, which is the wedge arriving through
        # the cleanup path instead of the work.
        await _delete(batch, name, namespace)

    return {**result, "output": output, **({"extra": extra} if extra_logs
                                           else {})}


def _ended(pod, name: str, job_status, container: str,
           namespace: str = "") -> dict:
    """A failed Job, read through its pod.

    THE JOB STATUS ALONE CANNOT ANSWER THIS. `failed` with
    `BackoffLimitExceeded` is what a Job says for a criterion that
    returned 1, for one whose image would not pull, and for one whose
    node was reclaimed. Only the POD carries the difference, so this
    reads it — and reads the REAL EXIT CODE off it too, which the old
    code did not: it hardcoded 1, so `from_exit`'s 127 branch could
    never fire on this substrate. A criterion in an image with no pytest
    was recorded as `fail` — the exact inversion G0 found and fixed on
    the Docker path, silently reintroduced here.
    """
    if pod is None:
        # THE POD IS WHAT IS MISSING, NOT THE RECORD. The old wording
        # here -- "there is no record of what the criterion did" --
        # made this the end of the investigation rather than the start
        # of one, three times in a day. The classification really is
        # lost, because only the pod's containerStatuses carry the
        # difference between a criterion that returned 1 and an image
        # that would not pull; the OUTPUT is not, and saying so is the
        # whole difference between a dead end and a query.
        return _infra(
            "the Job failed and its pod has already been deleted, so its "
            "exit status cannot be classified here — "
            + logs_are_in_cloud_logging(job=name, namespace=namespace,
                                        container=container),
            # UNOBSERVED, and NOT row 3 even though a reclaimed node is
            # one common way to reach here. The pod is what is missing,
            # so there is no evidence of who ended the run, and claiming
            # a disruption on no evidence buys free re-runs.
            UNOBSERVED)

    verdict = classify(pod, container)
    if verdict["infra_error"]:
        logger.warning("job %s: %s", name, verdict["infra_error"])
        return {"exit_code": None, "timed_out": False,
                "infra_error": verdict["infra_error"]}

    # A DEADLINE KILL PRODUCES AN EXIT CODE TOO — the signal's, not the
    # criterion's — so the deadline outranks it. Asked in this order and
    # not the other, because a pod that never started ALSO hits the
    # deadline, and calling that a timeout tells the executor its suite
    # is slow when the registry was down.
    return {"exit_code": verdict["exit_code"],
            "timed_out": _deadline_exceeded(job_status),
            "infra_error": None}


def _infra(reason: str, kind: str = UNKNOWN) -> dict:
    return {"exit_code": None, "timed_out": False,
            "infra_error": InfraError(reason, kind)}


def _deadline_exceeded(status) -> bool:
    for cond in (status.conditions or []):
        if cond.type == "Failed" and cond.reason == "DeadlineExceeded":
            return True
    return False


async def _find_pod(core, name: str, namespace: str):
    """A Job's pod, by label. Kubernetes appends a suffix to the name."""
    pods = await quiet(core.list_namespaced_pod,
                       what=f"reading the pod for job {name}",
                       namespace=namespace,
                       label_selector=f"job-name={name}")
    if pods is None:
        return None
    return pods.items[0] if pods.items else None


async def _logs(core, pod, namespace: str, container: str,
                job: str = "") -> str:
    """One container's output, or an explanation. ALWAYS `str`.

    Reading logs must never be fatal: the exit code is the verdict and
    the output is evidence for a person, so failing to collect it
    degrades the report rather than the result.

    AND IT MUST BE DECODED HERE, which is the whole of the bug measured
    on execution 207. The annotation said `str` and the client handed
    back `bytes`, so this returned bytes to every caller. The output
    then rendered as a `b'...'` repr in logs -- ugly but harmless -- and
    did something far worse to the capture sidecar's RETURN CHANNEL:

        parts[0] == MARKER    # b'AGENTOS_CAPTURE_ID' == 'AGENTOS...'

    In Python 3 bytes never equal str, so `artifact_id_from` found
    nothing and a capture that had WORKED -- 45,864,960 bytes stored,
    work exited 0, the id printed on its own line -- was reported as
    "the capture sidecar stored nothing". Every environment replay and
    every setup preparation failed that way, for a reason that was
    printed correctly at every step.

    Decoded at the seam rather than at each reader: there is one place
    logs enter this process, and a caller that has to remember to decode
    is the same bug waiting for its next caller.
    """
    # AND THE EXPLANATION MUST NOT BE A LIE, which both of these were
    # until 2026-09-17. "its output is unavailable" and "(log
    # unavailable)" assert something about the EVIDENCE; what is
    # actually true is something about this process's reach. The pod is
    # deleted by `run_job`'s own `finally` on every single run, so the
    # first of these is the ORDINARY path after a race, not an
    # exception -- and it was telling every reader that the log they
    # were about to go and find does not exist. It does; see
    # k8s_logref.
    if pod is None:
        return "(" + logs_are_in_cloud_logging(
            job=job, namespace=namespace, container=container) + ")"
    # LOGS_S, not the ordinary budget: how long this takes is the size
    # of the work's own output, not the API server's health.
    out = await quiet(core.read_namespaced_pod_log,
                      what=f"reading {container} logs",
                      budget=LOGS_S, name=pod.metadata.name,
                      namespace=namespace, container=container)
    if out is None:
        # The pod's NAME is the one thing this branch has that the
        # branch above does not, and it is the exact key Cloud Logging
        # indexes on -- so the reader gets an exact query rather than a
        # prefix match.
        return "(" + logs_are_in_cloud_logging(
            pod=pod.metadata.name, namespace=namespace,
            container=container) + ")"
    return as_text(out)


def as_text(out) -> str:
    """Whatever the client returned, as readable text.

    TWO SHAPES, AND THE SECOND IS THE NASTY ONE.

    `bytes` is the obvious case. The other, measured on execution 208
    after the obvious case was already fixed, is a `str` WHOSE CONTENT
    IS A BYTES REPR -- the client stringified the response body instead
    of decoding it, so the value is literally

        b'AGENTOS_CAPTURE_ID 07c760fc...\\ncapture: 45864960 bytes...\\n'

    with a real `b`, real quotes, and BACKSLASH-n rather than newlines.
    `isinstance(out, bytes)` is False for it, `splitlines()` returns one
    line, and the marker is never found -- which is the same failure as
    before wearing a different type. Two rounds were spent on that,
    because the printed evidence looks identical either way.

    `literal_eval` and not `eval`: this is untrusted container output.
    It parses literals and nothing else, and anything it will not parse
    is returned unchanged.
    """
    if isinstance(out, (bytes, bytearray)):
        # `replace`, because a container may emit anything at all and a
        # UnicodeDecodeError here would lose the whole log -- including
        # the marker -- over one stray byte.
        return bytes(out).decode("utf-8", "replace")
    if not isinstance(out, str):
        return str(out)
    body = out.strip()
    if len(body) >= 3 and body[0] in "bB" and body[1] in "'\"" \
            and body[-1] == body[1]:
        try:
            unwrapped = ast.literal_eval(body)
        except (ValueError, SyntaxError):
            return out
        if isinstance(unwrapped, (bytes, bytearray)):
            return bytes(unwrapped).decode("utf-8", "replace")
    return out


async def _delete(batch, name: str, namespace: str) -> None:
    """In a finally. A verifier that leaks a Job per criterion is one
    nobody runs often — the same reasoning the Docker driver gives for
    removing its container."""
    await quiet(batch.delete_namespaced_job,
                what=f"deleting job {name}", name=name,
                namespace=namespace, propagation_policy="Background")
