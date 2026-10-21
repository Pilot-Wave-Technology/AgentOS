"""Which of these artifact ids the store can actually produce.

WHY A CHECK NEEDS THIS. `kernel.verifier.artifact_kind` answers an
`artifact` criterion from `kernel.artifact_manifest` -- it asks whether
something was EMITTED, never whether it can be READ. For two days that
gap held: five artifacts across four stories were registered as durable
while the bucket took nothing, and three stories reported "satisfied by
1 artifact(s)" for objects that did not exist. The check was green and
the deliverable was gone.

WHY THE POD CANNOT ANSWER IT ITSELF. The check runs on the WORKER
(`kernel/verifier/kinds.py`: "evaluate runs on the worker, where the
connection already is"), and the worker's own binding is
ARTIFACT_STORE=filesystem rooted on its workspace -- it is denied the
metadata server because its sandbox shares the network namespace. Since
artifacts now go to the bucket through the control panel, the bytes are
NOT on the pod's disk. A local `exists()` there would answer False for
every artifact that was stored perfectly well, and fail every story.

So the same party that wrote the bytes is asked whether they are there.

BATCHED, one round trip for a criterion's whole set of hits: a check is
on the completion path and N sequential round trips across a web tier is
latency a person waits through.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.internal_exec_auth import require_execution_token

log = logging.getLogger(__name__)

router = APIRouter()

# A criterion matches a handful of artifacts, not thousands. The cap is
# here so a malformed caller cannot turn one request into an unbounded
# walk of the bucket.
MAX_IDS = 64


@router.get("/internal/artifact-exists")
async def artifact_exists(request: Request) -> dict:
    """The subset of `id` that the artifact store does NOT hold."""
    raw = (request.query_params.get("execution_id") or "").strip()
    if not raw.isdigit():
        raise HTTPException(status_code=400,
                            detail="execution_id is required")
    require_execution_token(request, int(raw))

    ids = [i.strip() for i in request.query_params.getlist("id") if i.strip()]
    if not ids:
        raise HTTPException(status_code=400,
                            detail="at least one `id` is required")
    if len(ids) > MAX_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"{len(ids)} ids exceeds the {MAX_IDS} one criterion "
                   f"may ask about")

    try:
        from kernel.artifacts.aio import aexists

        missing = [i for i in ids if not await aexists(i)]
    except Exception as exc:                                # noqa: BLE001
        from kernel.artifacts.store import describe

        # 502, NOT AN EMPTY `missing`. A store that cannot be reached
        # must not read as "everything is present" -- that is the exact
        # shape of the bug this endpoint exists to close. The caller
        # turns this into an `error` outcome, never a pass and never a
        # fail.
        log.exception("artifact-exists failed for execution %s", raw)
        raise HTTPException(
            status_code=502,
            detail=f"asking {describe()} whether {len(ids)} artifact(s) "
                   f"are present: {exc}")

    if missing:
        log.warning("execution %s: %d catalogued artifact(s) are not in "
                    "the store: %s", raw, len(missing),
                    ", ".join(i[:12] for i in missing))
    return {"missing": missing, "checked": len(ids)}
