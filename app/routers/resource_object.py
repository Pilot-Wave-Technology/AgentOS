"""One granted document's bytes, for the process that builds workspaces.

WHY THIS EXISTS. A story granted a document gets it copied into the
executor's workspace at `docs/<name>`. The document lives in the
control panel's object store; the process that builds the workspace is
the kernel worker, which runs in a container with no `app/`, no cloud
credentials, and a standing rule that it stays cloud-free.

So the kernel asks, and the control panel answers — the same shape as
`/internal/git-credential`, which exists for the same reason: the
capability belongs to whoever holds the credential, and the kernel
holds none. The alternative was giving the E0 container GCS access,
which would trade a documented boundary for a convenience.

SCOPED TO A GRANT, NOT TO A URI. The caller names a resource and a
story, and this checks the story actually holds that resource before
returning a byte of it. Taking a bare `gs://` URI would make this an
open proxy for the resource bucket to anyone who can reach the internal
port.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.database import get_pool
from app.internal_auth import verify_internal_bearer

log = logging.getLogger(__name__)

router = APIRouter()

# A document is a brief, not a dataset dump. Something larger than this
# is not what the executor should be reading inline, and a workspace
# build should not stall on it.
MAX_BYTES = 25 * 1024 * 1024


@router.get("/internal/resource-object")
async def resource_object(request: Request) -> Response:
    """The bytes of one document this story was granted."""
    verify_internal_bearer(request)

    story_id = (request.query_params.get("story_id") or "").strip()
    resource_id = (request.query_params.get("resource_id") or "").strip()
    if not story_id or not resource_id:
        raise HTTPException(status_code=400,
                            detail="story_id and resource_id are required")

    # THE GRANT IS THE AUTHORISATION. Not the URI, and not the caller's
    # word: a story may read what it was granted, and nothing else.
    row = await get_pool().fetchrow(
        """
        SELECT gr.name,
               COALESCE(NULLIF(gr.clone_url, ''), NULLIF(gr.uri, ''),
                        NULLIF(gr.gcs_uri, '')) AS uri
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1 AND sr.resource_id = $2
        """,
        story_id, resource_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"story {story_id} was not granted resource "
                   f"{resource_id}")
    if not row["uri"]:
        raise HTTPException(
            status_code=404,
            detail=f"{row['name']!r} has no stored object to read")

    try:
        from app.storage import download_resource_uri
        blob = download_resource_uri(row["uri"])
    except Exception as exc:                                # noqa: BLE001
        # Named, because the executor's workspace build fails on this
        # and "could not fetch" with no cause sent one person hunting
        # through IAM for an hour.
        log.exception("resource-object: %s (%s)", resource_id, row["uri"])
        raise HTTPException(status_code=502,
                            detail=f"fetching {row['name']!r}: {exc}")

    if len(blob) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{row['name']!r} is {len(blob):,} bytes; the workspace "
                   f"copies documents up to {MAX_BYTES:,}")
    return Response(content=blob, media_type="application/octet-stream")
