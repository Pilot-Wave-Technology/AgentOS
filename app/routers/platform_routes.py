"""User-facing Platform Research Council routes — the "Run research sweep" trigger
and a status endpoint for the running indicator. Admin-only (the __platform__
goal is admins-only; require_goal_member grants admins access to it)."""
import logging

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import require_goal_member
from app.config import get_settings
from app.database import get_pool
from app.services.platform_council import PLATFORM_GOAL_ID
from app.services.platform_council import runs as _runs

log = logging.getLogger(__name__)
router = APIRouter()


def _enabled() -> bool:
    return bool(getattr(get_settings(), "platform_council_enabled", False))


async def _platform_thread_id() -> str | None:
    pool = get_pool()
    return await pool.fetchval("SELECT go_thread_id FROM goals WHERE id=$1", PLATFORM_GOAL_ID)


@router.post("/goals/{pid}/research/run")
async def run_full_research(pid: str, request: Request, user: dict = Depends(require_goal_member)):
    """Trigger the Independent Platform Research discovery sweep — a breadth-first scan
    of what shipped in the last ~2 weeks across model/tool/skill, logged to the
    discovery feed. The daily deep-dive scheduler then researches those in depth."""
    if pid != PLATFORM_GOAL_ID:
        raise HTTPException(status_code=404, detail="not the platform council goal")
    if not _enabled():
        return JSONResponse({"ok": False, "reason": "Platform Research Council is disabled "
                             "on this deployment."}, status_code=403)
    from app.services.platform_council import worker as _worker
    res = await _worker.spawn_discovery(trigger="manual")
    return JSONResponse(res, status_code=200 if res.get("ok") else 409)


@router.get("/goals/{pid}/research/status")
async def research_status(pid: str, request: Request, user: dict = Depends(require_goal_member)):
    """Latest sweep status — drives the thread-page 'council running' indicator."""
    if pid != PLATFORM_GOAL_ID:
        raise HTTPException(status_code=404, detail="not the platform council goal")
    pool = get_pool()
    sweep = await pool.fetchrow(
        "SELECT id, trigger, status, kinds, producers_done, created_at, finished_at "
        "FROM platform_sweeps ORDER BY created_at DESC LIMIT 1"
    )
    if not sweep:
        return JSONResponse({"running": False})
    running = sweep["status"] in ("researching", "reviewing")
    kinds = list(sweep["kinds"] or [])
    done = list(sweep["producers_done"] or [])
    return JSONResponse({
        "running": running,
        "status": sweep["status"],
        "phase": sweep["status"] if running else None,
        "researching": sweep["status"] == "researching",
        "reviewing": sweep["status"] == "reviewing",
        "producers_progress": f"{len(done)}/{len(kinds)}" if kinds else None,
        "sweep_id": sweep["id"],
        "trigger": sweep["trigger"],
    })
