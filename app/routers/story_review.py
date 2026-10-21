"""One story review, recorded by the side of the wall that may write it.

WHY THIS EXISTS, and it is the third instance of one pattern. The
execution pod holds no cloud credential, so `/internal/resource-object`
reads a granted document for it and `/internal/artifact` stores an
emitted one. It also holds no DATABASE privilege outside the `kernel`
schema: its DSN is assembled in the pod from an `execution-db` Secret,
a deliberately scoped execution identity, and `story_reviews` and
`stories` are public-schema tables it cannot touch.

Measured: execution 245's reviewer produced a good review three times
and every submission died on

    asyncpg.exceptions.InsufficientPrivilegeError:
    permission denied for table story_reviews

THE ANSWER IS NOT A GRANT. The pod is the least-trusted process in the
system -- its sandbox runs model-authored code and shares its network
namespace -- and `UPDATE` on `stories` is not a privilege to hand it so
that a convenience can stay convenient. The same reasoning kept the
bucket credential out of it.

SO THE TRANSITION DOES CROSS A NETWORK HOP, which the first version of
`record_review` argued against: "the transition is the thing that must
not fail". That argument was right about the stakes and wrong about the
remedy -- the pod could not make the write at all. What makes the hop
acceptable is that the failure is already covered: `review_deadline`
sweeps `done` stories with no review and records `not_produced` from the
control panel, which is L4 and exists precisely for the case where the
pod cannot speak.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.internal_exec_auth import require_execution_token

log = logging.getLogger(__name__)

router = APIRouter()

# A review is a page of prose and a handful of findings. Larger than this
# is a reviewer pasting the diff back, which helps nobody and would push
# a multi-megabyte body through a web tier.
MAX_BYTES = 2 * 1024 * 1024


@router.post("/internal/story-review")
async def story_review(request: Request) -> dict:
    """Record the verdict for one execution's story, and hand it on."""
    raw = (request.query_params.get("execution_id") or "").strip()
    if not raw.isdigit():
        raise HTTPException(status_code=400,
                            detail="execution_id is required")
    execution_id = int(raw)

    # THE EXECUTION'S OWN TOKEN. Not the shared internal bearer: this is
    # called from the WORKER container, which runs alongside the sandbox
    # and shares its network namespace, so a token that opens every
    # internal route has no business there. Same check the artifact
    # endpoint uses.
    require_execution_token(request, execution_id)

    body = await request.body()
    if len(body) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{len(body):,} bytes is more review than a person "
                   f"will read; the cap is {MAX_BYTES:,}")
    try:
        payload = await request.json()
    except Exception as exc:                                # noqa: BLE001
        raise HTTPException(status_code=400,
                            detail=f"body is not JSON: {exc}")

    verdict = str(payload.get("verdict") or "").strip()
    text = str(payload.get("review_text") or "").strip()
    if not verdict or not text:
        raise HTTPException(
            status_code=400,
            detail="both `verdict` and `review_text` are required")

    from app.database import get_pool
    from kernel.worker.review_finish import record_review

    async with get_pool().acquire() as conn:
        ok = await record_review(
            conn, execution_id, verdict=verdict, review_text=text,
            reviewer=str(payload.get("reviewer") or f"reviewer-{execution_id}"))
    if not ok:
        # NOT AN ERROR THE REVIEWER CAN ACT ON. Its run has no story, or
        # the story went away while it was reading. Saying so plainly
        # beats a 500 that sends somebody looking at the endpoint.
        raise HTTPException(
            status_code=409,
            detail=f"execution {execution_id} has no story to review")

    log.info("story review recorded for execution %s: %s",
             execution_id, verdict)
    return {"recorded": True, "verdict": verdict}
