"""D2 — a person can see and download what a run produced.

THE GAP. An errand's deliverable IS its artifacts, and until now the only
way to get at one was to be an agent holding `read_artifact`. A person
could read an agent's SUMMARY of a report and never the report.

That is the wrong way round. The artifacts are the work; the agent's
account of them is the convenience. And the account cannot be trusted
without the original — this round found BOTH agents describing a report's
contents having never opened it, which is undetectable unless somebody
can check.

WHICH ERRAND MADE IT travels with every row, because the first question
anybody asks of a finding is where it came from, and a list of four files
called `report.md` answers nothing.

MEMBERSHIP, NOT AUTHENTICATION. Artifacts are goal-scoped work product,
so `_require_member` gates every route — the same helper the kernel-park
endpoints use, and 404 rather than 403 for the same reason: confirming a
goal exists to somebody who cannot see it is itself a disclosure.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.auth import get_current_user
from app.database import get_pool
from app.routers.kernel_park import _require_member

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/goals/{pid}/artifacts")
async def list_goal_artifacts(pid: str, story: str = "", role: str = "",
                              user: dict = Depends(get_current_user)):
    """Everything this goal's runs have produced, newest first.

    Reads the MANIFEST rather than the catalogue: the manifest is the
    record of every emission including roles the catalogue deliberately
    leaves out (`log`, `figure`), and a person looking for what a run
    produced wants all of it.
    """
    await _require_member(str(user.get("uid") or ""), pid)
    rows = await get_pool().fetch(
        """
        SELECT m.id, m.artifact_id, m.name, m.role, m.media_type,
               m.bytes, m.summary, m.supersedes, m.created_at,
               e.id AS execution_id, e.kind AS run_kind,
               COALESCE(e.story_id, m.story_id) AS story_id,
               s.title AS story_title, s.work_kind,
               r.id AS resource_id
          -- LEFT JOIN + COALESCE: an artifact may belong to an EXECUTION (a pod
          -- run) or directly to a STORY (a run in somebody's editor, which
          -- creates no execution). An inner join on executions filtered every
          -- local artifact out silently -- stored, catalogued, invisible.
          FROM kernel.artifact_manifest m
          LEFT JOIN kernel.executions e ON e.id = m.execution_id
          LEFT JOIN stories s ON s.id = COALESCE(e.story_id, m.story_id)
          LEFT JOIN goal_resources r
                 ON r.id = 'art_' || left(m.artifact_id, 12)
                AND r.goal_id = COALESCE(e.goal_id, m.goal_id)
         WHERE COALESCE(e.goal_id, m.goal_id) = $1
           AND ($2 = '' OR COALESCE(e.story_id, m.story_id) = $2)
           AND ($3 = '' OR m.role = $3)
         ORDER BY m.id DESC
         LIMIT 200
        """, pid, story, role)

    return {"goal": pid, "count": len(rows), "artifacts": [{
        "artifact_id": r["artifact_id"],
        "name": r["name"], "role": r["role"],
        "media_type": r["media_type"], "bytes": r["bytes"],
        "summary": r["summary"],
        # SUPERSEDED VERSIONS ARE KEPT AND MARKED, not hidden: a person
        # comparing what a re-run changed needs both, and a list that
        # silently drops the old one makes that impossible.
        "supersedes": r["supersedes"],
        "created_at": r["created_at"],
        # WHO MADE THIS — the whole point of the row.
        "produced_by": {
            "execution_id": r["execution_id"],
            "kind": r["work_kind"] or r["run_kind"],
            "story_id": r["story_id"],
            "story_title": r["story_title"],
        },
        # Present when the artifact was catalogued, so later work can be
        # granted it by name. Absent for roles that are not (log, figure).
        "resource_id": r["resource_id"],
        "download": f"/goals/{pid}/artifacts/{r['artifact_id']}/download",
    } for r in rows]}


@router.get("/goals/{pid}/artifacts/{artifact_id}/download")
async def download_artifact(pid: str, artifact_id: str,
                            user: dict = Depends(get_current_user)):
    """The bytes, as a file.

    THE STORE IS CONTENT-ADDRESSED AND GOAL-BLIND — it is keyed by hash
    alone, so it cannot enforce who may read what. The manifest join
    below is what scopes this to the goal: an id belonging to another
    goal's run is a 404 here even though the bytes exist.
    """
    await _require_member(str(user.get("uid") or ""), pid)

    row = await get_pool().fetchrow(
        """
        SELECT m.name, m.media_type, m.bytes,
               COALESCE(e.story_id, m.story_id) AS story_id, e.kind
          FROM kernel.artifact_manifest m
          LEFT JOIN kernel.executions e ON e.id = m.execution_id
         WHERE m.artifact_id = $1 AND COALESCE(e.goal_id, m.goal_id) = $2
         LIMIT 1
        """, artifact_id, pid)
    if row is None:
        raise HTTPException(status_code=404, detail="No such artifact")

    from kernel.artifacts.aio import aget
    from kernel.artifacts.store import ArtifactNotFound

    try:
        # OFF THE EVENT LOOP. This is an `async def`, and the store's
        # methods are synchronous — free under the filesystem binding,
        # a network round trip against a bucket. A blocking download
        # here stalls every other request this worker is serving for the
        # length of it, and shows up as unrelated timeouts rather than
        # as anything wrong with this route.
        data = await aget(artifact_id)
    except (ArtifactNotFound, OSError) as exc:
        log.warning("artifact %s is in the manifest and not in the store: %s",
                    artifact_id[:12], exc)
        # A MANIFEST ROW WITHOUT BYTES IS A DIFFERENT FAULT from a
        # missing row, and saying so saves somebody checking permissions
        # when the store is what is wrong.
        raise HTTPException(
            status_code=410,
            detail="recorded but the content is no longer in the store") \
            from None

    from app.services.content_disposition import content_disposition

    name = row["name"] or f"{artifact_id[:12]}.bin"
    return Response(
        content=data,
        media_type=row["media_type"] or "application/octet-stream",
        headers={
            # Same latin-1 trap as the resource route: an artifact
            # named by a model routinely has an em dash in it.
            "Content-Disposition": content_disposition("attachment", name),
            # Provenance on the response itself, so a downloaded file can
            # be traced back without the page it came from.
            "X-Produced-By-Story": row["story_id"] or "",
            "X-Produced-By-Kind": row["kind"] or "",
        })
