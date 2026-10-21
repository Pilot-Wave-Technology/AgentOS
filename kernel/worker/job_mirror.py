"""Reporting a long job's state to the one process that may record it.

THE FOURTH INSTANCE OF ONE PATTERN. The pod holds no cloud credential,
so `/internal/resource-object` reads a granted document for it and
`/internal/artifact` stores an emitted one; it holds no database
privilege outside its own scope, so `/internal/story-review` records a
review for it. This is the same seam for the same reason: the pod's
sandbox runs model-authored code and shares the pod's network namespace,
and write privilege on a public-schema table is not a convenience to
hand it.

Execution 245 is the measured case. Its reviewer produced a good review
three times and every submission died on `permission denied for table
story_reviews`, then crashed the leg on the way out.

THROTTLED, BECAUSE THIS IS THE ONE THAT REPEATS. The other three fire
once. A job's state changes continuously, and a POST per poll on a
two-hour job would be thousands of round trips for a number a person
reads occasionally. Start and end always; in between, only when the
throttle has elapsed. "Last byte twelve seconds ago" is as useful as
"last byte two seconds ago" to somebody asking how it is going.

NEVER RAISES. A mirror that fails is a question the assistant cannot
answer; a mirror that throws is a leg that dies holding a job. The
former is a degradation and the latter is the thing this phase exists
to prevent, so every path here swallows and logs.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# How often an in-flight job re-reports. Start and finish ignore this.
#
# THIRTY SECONDS, against a fifteen-second poll: the mirror is written
# every other internal poll, which is fresh enough for a person asking
# and costs ~240 writes on a two-hour job rather than ~480.
THROTTLE_S = 30.0

ROUTE = "execution-jobs"

_last_sent: dict[str, float] = {}


def _due(job_id: str, running: bool) -> bool:
    """Start and end always; the middle on the throttle."""
    if not running:
        _last_sent.pop(job_id, None)
        return True
    now = time.monotonic()
    previous = _last_sent.get(job_id)
    if previous is not None and now - previous < THROTTLE_S:
        return False
    _last_sent[job_id] = now
    return True


def report(execution_id: int, snap: dict, *, force: bool = False) -> bool:
    """POST one job's state. Returns whether it was sent.

    SYNCHRONOUS AND urllib, for the reason `emit._put_over_http` gives:
    the pod's image carries no cloud SDK and no HTTP library beyond the
    standard one. Callers run it through a thread.
    """
    import json
    import urllib.error
    import urllib.request

    from kernel.workspace import broker, podfs_auth

    job_id = snap.get("id") or ""
    if not job_id:
        return False
    if not force and not _due(job_id, bool(snap.get("running"))):
        return False

    url = broker.endpoint(ROUTE, "EXECUTION_JOBS_URL")
    token = (os.environ[podfs_auth.ENV_VAR]
             if podfs_auth.ENV_VAR in os.environ else "")
    if not url or not token:
        # NOT AN ERROR. A local run with no panel is the ordinary case on
        # E0, and a job that runs perfectly while nothing records it is
        # better than a job that refuses to run because nothing is
        # listening.
        return False

    body = json.dumps({
        "job_id": job_id,
        "command": snap.get("command") or "",
        "produced": int(snap.get("produced") or 0),
        "running": bool(snap.get("running")),
        "exit_code": snap.get("exit_code"),
        "ended": snap.get("ended"),
        # A7. Empty on every report but the first and the last -- the
        # panel treats an empty string as "no news", never as an erasure.
        # The story is resolved panel-side from the execution.
        "fingerprint": snap.get("fingerprint") or "",
        "fingerprint_end": snap.get("fingerprint_end") or "",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{url}?execution_id={execution_id}", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            answer.read()
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        logger.warning("job mirror: refused (%s): %s", exc.code, detail)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("job mirror: could not reach the panel: %s", exc)
    return False
