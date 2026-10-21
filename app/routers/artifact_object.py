"""One emitted artifact's bytes, written where they outlive the pod.

WHY THIS EXISTS, and it is the mirror of `/internal/resource-object`
next door. That route exists because the process that BUILDS a workspace
holds no cloud credentials, so it asks the control panel to read. This
one exists because the process that EMITS an artifact holds none either,
and it needs the control panel to write.

WHAT WENT WRONG WITHOUT IT. `emit_artifact` runs in the leg, and since
the leg-in-pod cutover the leg runs inside the execution pod
(`kernel/worker/pod_main.py`: "the workspace surface AND the leg loop").
That pod is given ARTIFACT_STORE=filesystem rooted on its own workspace,
deliberately -- it is denied the metadata server, because its sandbox
shares the network namespace and a credential there is one a model can
mint.

So `aput` SUCCEEDED. It returned a real content hash, wrote the bytes to
the workspace PVC, and `emit_artifact` went on to register a goal
resource promising something durable, content-addressed and mountable
into later runs. Then the pod ended and the bytes went with it.

Measured: the artifact bucket's newest object was 2026-09-18T17:26, and
every artifact emitted after that -- five of them across four stories --
existed only as a manifest row. Nobody noticed because an artifact CHECK
is satisfied from that row: it asks whether something was emitted, never
whether it can be read. Three stories reported "satisfied by 1
artifact(s)" for objects that did not exist. It surfaced when a later
story was granted one and its workspace build failed.

SCOPED TO THE EXECUTION, NOT TO A BUCKET PATH. The caller names the run
it is writing for, and this checks that run exists and is not finished
before storing a byte. Taking a destination would make this an open
write proxy for the artifact bucket to anyone who reaches the internal
port -- the same reasoning the read side gives for taking a grant rather
than a URI.

CONTENT-ADDRESSED, so this cannot overwrite. The id IS the hash of the
bytes; writing the same content twice is the same object, and writing
different content cannot collide with an existing artifact. That is what
makes a write endpoint safe to expose where a `PUT <path>` would not be.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.database import get_pool
from app.internal_exec_auth import require_execution_token

log = logging.getLogger(__name__)

router = APIRouter()

# An artifact is a report, a log or a tree -- not a dataset. The read
# side caps documents at 25 MB for the same reason; this is larger
# because a captured tree is legitimately bigger than a brief, and still
# bounded because the bytes cross a web tier to get here.
MAX_BYTES = 100 * 1024 * 1024

# States in which a run may still produce an artifact. A finished run
# writing one is either a bug or a replay, and both are worth refusing
# rather than accepting into a catalogue somebody will trust.
OPEN_STATES = ("PENDING", "ADMITTED", "DISPATCHING", "RUNNING",
               "PARKED_HOT", "PARKED_COLD")


@router.post("/internal/artifact")
async def put_artifact(request: Request) -> dict:
    """Store one artifact's bytes and return its content id."""
    raw = (request.query_params.get("execution_id") or "").strip()
    if not raw.isdigit():
        raise HTTPException(status_code=400,
                            detail="execution_id is required")
    execution_id = int(raw)

    body = await request.body()
    if not body:
        raise HTTPException(
            status_code=400,
            detail="an artifact with no bytes is a manifest row with "
                   "nothing behind it, which is the failure this "
                   "endpoint exists to end")
    if len(body) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{len(body):,} bytes exceeds the {MAX_BYTES:,} an "
                   f"artifact may be")

    # THE EXECUTION'S OWN TOKEN IS THE AUTHORISATION, not a global
    # bearer and not the caller's word.
    #
    # The worker already holds `AGENTOS_PODFS_TOKEN`, derived by the
    # conductor at spec build from AGENT_TOKEN_SIGNING_KEY and this
    # execution's id -- "the worker has the answer and not the key". The
    # control panel holds the key, so it can recompute what execution N
    # must present and compare.
    #
    # WHY NOT `verify_internal_bearer`. That checks a single shared
    # secret, and the pod does not have one -- deliberately: a token in
    # the execution pod that opens every internal route is a token the
    # sandbox shares a network namespace with. This proves exactly one
    # thing, which is the one thing that matters here: the caller is the
    # run it claims to be writing for.
    require_execution_token(request, execution_id)

    state = await get_pool().fetchval(
        "SELECT state FROM kernel.executions WHERE id = $1", execution_id)
    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"no execution {execution_id}")
    if state not in OPEN_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"execution {execution_id} is {state}; a finished run "
                   f"does not emit artifacts")

    # THE CONTROL PANEL'S OWN STORE, which is the whole point: it holds
    # the credential the pod is denied. The binding honoured here is
    # this service's, and here that is gcs.
    #
    # THROUGH `aput`, NOT `get_store().put`, and the difference is the
    # whole reason that facade exists: a synchronous put inside an
    # `async def` is up to MAX_BYTES of upload with the event loop
    # stopped -- "not slowed, STOPPED". On the control panel that is
    # every other request in the process, including the scheduler beats
    # we just spent a week giving a home. The first draft of this
    # endpoint had it; a guard caught it.
    try:
        from kernel.artifacts.aio import aput

        artifact_id = await aput(body)
    except Exception as exc:                                # noqa: BLE001
        from kernel.artifacts.store import describe

        log.exception("artifact put failed for execution %s", execution_id)
        # NAMES THE STORE IT TRIED. The read side learned this the hard
        # way: a refusal that does not say where it looked sends the
        # reader to the writer and costs an afternoon.
        raise HTTPException(
            status_code=502,
            detail=f"storing {len(body):,} bytes in {describe()}: {exc}")

    log.info("artifact %s stored for execution %s (%d bytes)",
             artifact_id[:12], execution_id, len(body))
    return {"artifact_id": artifact_id, "bytes": len(body)}
