"""User-facing Security Council routes — manual trigger + status.

POST /goals/{pid}/security/run     — trigger a security review of a branch resource
GET  /goals/{pid}/security/status  — latest security run(s) for this goal's branches
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth import require_admin
from app.config import get_settings
from app.database import get_pool
from app.services.security.runs import resolve_branch_resource, enqueue_branch_review
from app.services.security.dispatcher import security_dispatcher

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/goals/{pid}/security/run")
async def trigger_security_review(pid: str, request: Request, user: dict = Depends(require_admin)):
    """Manually trigger a security review for this goal. platform.admin-only,
    regardless of goal membership.

    Body is optional:
      - {"resource_id": "<branch/repo resource>"}  → review that one branch.
      - {} (no body)                                → review EVERY active branch
        resource on the goal (one run each). This is what the goal-thread button
        sends, so a single click covers the goal regardless of branch count.

    Deduped per branch + global single-run (DB-enforced).

    Gated on security_ui_enabled: when the Security Council manual trigger is
    disabled on this deployment (e.g. shipped dormant on prod), it refuses rather
    than enqueuing runs that no dispatcher would process.
    """
    if not getattr(get_settings(), "security_ui_enabled", False):
        return JSONResponse(
            {"ok": False, "reason": "The Security Council is disabled on this deployment."},
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    resource_id = str(body.get("resource_id") or "").strip()
    pool = get_pool()
    created_by = user.get("uid") or user.get("id")

    # Resolve the target branch resource id(s).
    if resource_id:
        owns = await pool.fetchval(
            "SELECT 1 FROM goal_resources WHERE id = $1 AND goal_id = $2", resource_id, pid,
        )
        if not owns:
            return JSONResponse({"ok": False, "reason": "resource not found on this goal."}, status_code=404)
        target_ids = [resource_id]
    else:
        rows = await pool.fetch(
            "SELECT id FROM goal_resources WHERE goal_id = $1 AND kind = 'branch' AND status = 'active' "
            "AND clone_url <> '' AND branch_name <> '' ORDER BY created_at",
            pid,
        )
        target_ids = [r["id"] for r in rows]
        if not target_ids:
            return JSONResponse(
                {"ok": False, "reason": "This goal has no branch resources to review. "
                 "Attach a branch resource first."},
                status_code=400,
            )

    queued: list[dict] = []
    skipped: list[dict] = []
    for rid in target_ids:
        branch = await resolve_branch_resource(rid)
        if not branch:
            skipped.append({"resource_id": rid, "reason": "not a usable repo/branch (needs clone_url + branch)"})
            continue
        result = await enqueue_branch_review(branch, created_by=created_by)
        if result.get("created"):
            queued.append({"branch": branch["branch_name"], "run_id": result["run_id"]})
        else:
            skipped.append({"branch": branch["branch_name"], "reason": result.get("reason")})

    if queued:
        security_dispatcher.trigger()

    return JSONResponse(
        {"ok": bool(queued), "queued": queued, "skipped": skipped, "total_branches": len(target_ids)},
        status_code=200 if queued else 409,
    )


@router.get("/goals/{pid}/security/status")
async def security_status(pid: str, request: Request, user: dict = Depends(require_admin)):
    """Latest security run + stages for each branch this goal references."""
    pool = get_pool()
    branches = await pool.fetch(
        "SELECT DISTINCT lower(clone_url) AS lc, lower(branch_name) AS lb, clone_url, branch_name "
        "FROM goal_resources WHERE goal_id = $1 AND kind = 'branch' AND status = 'active' "
        "AND clone_url <> '' AND branch_name <> ''",
        pid,
    )
    out = []
    for b in branches:
        run = await pool.fetchrow(
            "SELECT id, status, trigger, head_sha, created_at, finished_at FROM security_review_runs "
            "WHERE lower(clone_url) = $1 AND lower(branch_name) = $2 ORDER BY created_at DESC LIMIT 1",
            b["lc"], b["lb"],
        )
        entry = {"branch": b["branch_name"], "clone_url": b["clone_url"], "run": None, "stages": []}
        if run:
            stages = await pool.fetch(
                "SELECT aspect, status, error, finished_at FROM security_review_stages "
                "WHERE run_id = $1 ORDER BY created_at",
                run["id"],
            )
            findings = await pool.fetch(
                "SELECT severity, COUNT(*) AS n FROM security_findings WHERE run_id = $1 GROUP BY severity",
                run["id"],
            )
            entry["run"] = {
                "id": run["id"], "status": run["status"], "trigger": run["trigger"],
                "head_sha": run["head_sha"],
                "created_at": run["created_at"].isoformat() if run["created_at"] else None,
                "finished_at": run["finished_at"].isoformat() if run["finished_at"] else None,
            }
            entry["stages"] = [
                {"aspect": s["aspect"], "status": s["status"], "error": s["error"],
                 "finished_at": s["finished_at"].isoformat() if s["finished_at"] else None}
                for s in stages
            ]
            entry["findings_by_severity"] = {f["severity"]: f["n"] for f in findings}
        out.append(entry)
    return JSONResponse({"branches": out})
