"""User-facing council routes — the manual trigger button on the GO page.

POST /goals/{pid}/council/run     — enqueue a council review for the goal
GET  /goals/{pid}/council/status  — latest run + stage statuses (for the button UI)
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth import require_admin
from app.database import get_pool
from app.services.council.runs import enqueue_run
from app.services.council.dispatcher import council_dispatcher

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/goals/{pid}/council/run")
async def trigger_council_review(pid: str, request: Request, user: dict = Depends(require_admin)):
    """Manually trigger a council review for this goal (deduped + global single-run).
    Goal Review Council is platform.admin-only, regardless of goal membership."""
    created_by = user.get("uid") or user.get("id")
    result = await enqueue_run(pid, created_by=created_by)
    if result.get("created"):
        council_dispatcher.trigger()
        return JSONResponse({"ok": True, "run_id": result["run_id"], "status": "queued"})
    return JSONResponse(
        {"ok": False, "run_id": result.get("run_id"), "reason": result.get("reason")},
        status_code=409,
    )


@router.get("/goals/{pid}/council/status")
async def council_status(pid: str, request: Request, user: dict = Depends(require_admin)):
    """Return the latest council run + its stage statuses for this goal."""
    pool = get_pool()
    run = await pool.fetchrow(
        "SELECT id, status, trigger, created_at, finished_at FROM council_review_runs "
        "WHERE goal_id = $1 ORDER BY created_at DESC LIMIT 1",
        pid,
    )
    if not run:
        return JSONResponse({"run": None, "stages": []})
    stages = await pool.fetch(
        "SELECT stage, status, story_id, error, finished_at FROM council_review_stages "
        "WHERE run_id = $1 ORDER BY created_at",
        run["id"],
    )
    return JSONResponse({
        "run": {
            "id": run["id"],
            "status": run["status"],
            "trigger": run["trigger"],
            "created_at": run["created_at"].isoformat() if run["created_at"] else None,
            "finished_at": run["finished_at"].isoformat() if run["finished_at"] else None,
        },
        "stages": [
            {
                "stage": s["stage"],
                "status": s["status"],
                "story_id": s["story_id"],
                "error": s["error"],
                "finished_at": s["finished_at"].isoformat() if s["finished_at"] else None,
            }
            for s in stages
        ],
    })
