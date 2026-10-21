"""Where a deleted pod's output actually is. One sentence, one place.

MEASURED 2026-09-17. `run_job` deletes the Job in a `finally` — by
design, because a verifier that leaks a Job per criterion is one nobody
runs — and every reader that arrived after the delete was told:

    (no pod found for this job; its output is unavailable)

THAT SENTENCE IS FALSE, and it is the expensive kind of false: it does
not say "I could not reach it", it says the output does not exist, so
nobody goes looking. Every container's stdout is shipped to Cloud
Logging, indexed by pod name, and outlives the pod by the bucket's
retention. Three separate investigations went at the wrong component in
one day on the strength of that one line.

SO THE TEXT IS A MODULE AND NOT A LITERAL. It lands in
`criterion_results.detail`, in the capture sidecar's failure tail and in
log lines, and the failure mode being fixed is precisely that the same
claim was spelled three times and each spelling was wrong on its own
terms. One function, one truth, and a test can assert against it.

The reader may be holding either name. A pod name is what Cloud Logging
indexes on; a Job name is a PREFIX of its pod's name, which the `:`
operator matches — worse than exact, and far better than nothing.
"""

from __future__ import annotations


def log_query(*, pod: str = "", job: str = "", namespace: str = "") -> str:
    """The gcloud command that reads this pod's output. Always runnable.

    `--freshness=1h` because the reader is nearly always looking at
    something that just happened; a person chasing an older run will
    widen it, and a default that returns nothing for the common case
    would teach them the logs are not there after all — which is the
    belief this whole module exists to correct.
    """
    match = (f'resource.labels.pod_name="{pod}"' if pod
             else f'resource.labels.pod_name:"{job}"')
    ns = f'resource.labels.namespace_name="{namespace}" ' if namespace else ""
    return ('gcloud logging read \'resource.type="k8s_container" '
            f'{ns}{match}\' --freshness=1h')


def logs_are_in_cloud_logging(*, pod: str = "", job: str = "",
                              namespace: str = "", container: str = "") -> str:
    """Why this reader has nothing, and where the output really is.

    Phrased as a statement about THIS PROCESS's reach and never about
    the output's existence. "Unavailable" was the word that cost the
    day: it reads as a property of the evidence rather than of the
    caller, and a reader who believes the evidence is gone stops.
    """
    who = f"the {container} container's output" if container else "its output"
    where = (f"pod {pod}" if pod
             else f"the pod for job {job} (its name starts with the job's)")
    return (f"this process could not read {who} from {where}, but the "
            f"output is not lost: every container's stdout goes to Cloud "
            f"Logging, indexed by pod name, and survives the pod. Read it "
            f"with: {log_query(pod=pod, job=job, namespace=namespace)}")
