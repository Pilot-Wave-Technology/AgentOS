"""What a long job is doing, recorded by the side of the wall that may write it.

THE FOURTH INSTANCE OF ONE PATTERN, and the docstring of
`app/routers/story_review.py` makes the whole argument. The pod holds no
cloud credential, so `/internal/resource-object` reads a granted
document for it and `/internal/artifact` stores an emitted one; it holds
no database privilege outside its own scope, so `/internal/story-review`
records a review for it. This records a job's state for it.

THE ANSWER IS NOT A GRANT. Execution 245's reviewer produced a good
review three times and every submission died on `permission denied for
table story_reviews`. The remedy there was this seam, not a wider role:
the pod's sandbox runs model-authored code and shares the pod's network
namespace, and write privilege on a public table is not a convenience to
hand it.

WHAT MAKES A MISSED WRITE SURVIVABLE. The other three routes carry
things that must not be lost -- a document, an artifact, a verdict. This
one carries a number a person reads occasionally, and a job that runs
perfectly while nothing records it is a degraded answer rather than lost
work. So every failure here is a warning and never a refusal to the pod.

SCHEMA-QUALIFIED, and that is not tidiness. Two kinds of connection
reach this code: the kernel's carry `search_path = kernel,public`, the
panel's pooled ones do not. Execution 246's reviewer reported its
verdict three times and every POST answered 500 on `relation
"executions" does not exist`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.database import get_pool
from app.internal_exec_auth import require_execution_token

log = logging.getLogger(__name__)

router = APIRouter()


async def record(conn, execution_id: int, payload: dict) -> bool:
    """Upsert one job's state. One row per job, updated in place.

    A job is a thing with a state, not a stream of events: the only
    question asked of it is what it is doing now.
    """
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        return False
    running = bool(payload.get("running"))
    # RESOLVED HERE, NOT SENT. The pod knows its execution and not
    # necessarily its story, and the panel is already holding the row
    # that joins them -- a field on the wire would be a second source of
    # truth for something one `SELECT` answers exactly.
    story_id = await conn.fetchval(
        "SELECT COALESCE(story_id, '') FROM kernel.executions WHERE id = $1",
        execution_id) or ""
    await conn.execute(
        """
        INSERT INTO kernel.execution_jobs
            (execution_id, job_id, story_id, command, produced, running,
             exit_code, ended, fingerprint, fingerprint_end,
             last_seen_at, updated_at)
        VALUES ($1, $2, $8, $3, $4, $5, $6, $7, $9, $10, now(), now())
        ON CONFLICT (execution_id, job_id) DO UPDATE SET
            story_id     = EXCLUDED.story_id,
            command      = EXCLUDED.command,
            produced     = EXCLUDED.produced,
            running      = EXCLUDED.running,
            exit_code    = EXCLUDED.exit_code,
            ended        = EXCLUDED.ended,
            -- A FINGERPRINT IS WRITTEN ONCE AND NEVER UNWRITTEN. The pod
            -- takes it at start and again at end; every report in between
            -- carries an empty string, and an empty string overwriting a
            -- recorded digest would erase the evidence mid-job.
            fingerprint     = CASE WHEN EXCLUDED.fingerprint <> ''
                              THEN EXCLUDED.fingerprint
                              ELSE kernel.execution_jobs.fingerprint END,
            fingerprint_end = CASE WHEN EXCLUDED.fingerprint_end <> ''
                              THEN EXCLUDED.fingerprint_end
                              ELSE kernel.execution_jobs.fingerprint_end END,
            -- LAST SEEN MOVES ONLY WHEN BYTES DID. Refreshing it on
            -- every report would make a silent job look healthy for
            -- ever, and "last byte 90 seconds ago while still running"
            -- is the AWOL signal this column exists for.
            last_seen_at = CASE
                WHEN EXCLUDED.produced > kernel.execution_jobs.produced
                THEN now() ELSE kernel.execution_jobs.last_seen_at END,
            updated_at   = now()
        """,
        execution_id, job_id, str(payload.get("command") or "")[:2000],
        int(payload.get("produced") or 0), running,
        payload.get("exit_code") if not running else None,
        payload.get("ended"), story_id[:200],
        str(payload.get("fingerprint") or "")[:4000],
        str(payload.get("fingerprint_end") or "")[:4000])
    return True


@router.post("/internal/execution-jobs")
async def report_job(request: Request, execution_id: int):
    """The pod reporting one of its jobs.

    Authenticated exactly as the other three internal pod routes are --
    an execution-scoped bearer the worker derives, never a shared token.
    """
    # THE EXECUTION'S OWN TOKEN, not the shared internal bearer. This is
    # called from the WORKER container, which runs alongside the sandbox
    # and shares its network namespace, so a token that opens every
    # internal route has no business there. The same check the artifact
    # and story-review endpoints use.
    require_execution_token(request, execution_id)

    try:
        payload = await request.json()
    except Exception:                                       # noqa: BLE001
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            written = await record(conn, execution_id, payload)
    except Exception as exc:                                # noqa: BLE001
        # A WARNING, NOT A 500 THE POD RETRIES. The pod throttles and
        # will report again; a failure here costs freshness, and a retry
        # storm against a database that is already unhappy costs more.
        log.warning("execution-jobs: could not record for %s: %s",
                    execution_id, exc)
        return JSONResponse({"recorded": False}, status_code=202)

    return JSONResponse({"recorded": written})
