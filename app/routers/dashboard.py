"""System + personal Dashboard page.

All data gathering lives in app/services/dashboard_metrics.py.
See app/templates/dashboard/README.md for the adapter-boundary rule.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.database import get_pool
from app.services import dashboard_metrics

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Valid tab identifiers in display order.
TABS = ["overview", "cost", "operations", "system", "taximeter", "task_queue", "my_work"]
DEFAULT_TAB = "overview"

# Prevents concurrent refresh runs — mat view REFRESH holds locks; running two
# in parallel would block and waste the second run entirely.
_refresh_lock = asyncio.Lock()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    user: dict = Depends(require_admin),
    tab: str = Query(default=DEFAULT_TAB),
    activity_page: int = Query(default=0, ge=0),
):
    active_tab = tab if tab in TABS else DEFAULT_TAB
    data = await dashboard_metrics.collect(user)
    return templates.TemplateResponse(
        "dashboard/index.html",
        {"request": request, "user": user, "active_tab": active_tab, "tabs": TABS,
         "activity_page": activity_page, **data},
    )


@router.get("/dashboard/body", response_class=HTMLResponse)
async def dashboard_body(
    request: Request,
    user: dict = Depends(require_admin),
    tab: str = Query(default=DEFAULT_TAB),
    activity_page: int = Query(default=0, ge=0),
):
    """Tab content target — full data gather, renders only the active tab's panels."""
    active_tab = tab if tab in TABS else DEFAULT_TAB
    data = await dashboard_metrics.collect(user)
    etag = f"{data['etag']}-{active_tab}-{activity_page}"

    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304)

    html = templates.TemplateResponse(
        "dashboard/partials/body.html",
        {"request": request, "user": user, "active_tab": active_tab, "tabs": TABS,
         "activity_page": activity_page, **data},
    )
    html.headers["ETag"] = etag
    # Correctness against manual refresh is handled client-side instead of by
    # shortening/dropping max-age: body.html tags every /dashboard/body fetch
    # with a `_v` query param equal to the last-known refreshed_at, and bumps
    # it the moment the Refresh button succeeds. A bumped `_v` makes the next
    # fetch a different URL, which the browser cache has never seen — always
    # a real network hit — while repeat visits with an unchanged `_v` keep
    # getting served from cache for the full 60s, same as before.
    html.headers["Cache-Control"] = "private, max-age=60"
    return html


@router.post("/dashboard/refresh")
async def refresh_dashboard(user: dict = Depends(require_admin)):
    """Force-refresh all dashboard materialized views in parallel.

    Admin-only. Returns the new refreshed_at timestamp so the UI can update
    the "as of X ago" label without a full page reload.
    """
    _log.info("dashboard.refresh requested uid=%s", user.get("uid"))
    if _refresh_lock.locked():
        _log.info("dashboard.refresh already running, rejecting duplicate uid=%s", user.get("uid"))
        return JSONResponse({"busy": True}, status_code=202)
    async with _refresh_lock:
        refreshed_at = await dashboard_metrics.refresh_materialized_views()
    return JSONResponse({"refreshed_at": refreshed_at.isoformat()})


# ── Per-panel lazy-loading endpoints ─────────────────────────────────────────
# Each renders a single partial with data from one materialized view read.
# Called by HTMX hx-trigger="load" on panel placeholder divs in body.html.

async def _panel_ctx(request: Request, user: dict, system_key: str, data: dict) -> dict:
    """Build the minimal template context needed by a system panel partial."""
    return {
        "request":     request,
        "user":        user,
        "is_operator": True,
        "system":      {system_key: data},
        "degraded":    data.pop("_degraded", []),
    }


@router.get("/dashboard/panel/cost", response_class=HTMLResponse)
async def panel_cost(request: Request, user: dict = Depends(require_admin)):
    data = await dashboard_metrics.fetch_panel_cost()
    ctx = await _panel_ctx(request, user, "cost", data)
    return templates.TemplateResponse("dashboard/partials/system_cost.html", ctx)


@router.get("/dashboard/panel/escalations", response_class=HTMLResponse)
async def panel_escalations(request: Request, user: dict = Depends(require_admin)):
    data = await dashboard_metrics.fetch_panel_escalations()
    ctx = await _panel_ctx(request, user, "escalations", data)
    return templates.TemplateResponse("dashboard/partials/system_escalations.html", ctx)


@router.get("/dashboard/panel/channels", response_class=HTMLResponse)
async def panel_channels(request: Request, user: dict = Depends(require_admin)):
    data = await dashboard_metrics.fetch_panel_channels()
    ctx = await _panel_ctx(request, user, "channels", data)
    return templates.TemplateResponse("dashboard/partials/system_channels.html", ctx)


@router.get("/dashboard/panel/approval-sla", response_class=HTMLResponse)
async def panel_approval_sla(request: Request, user: dict = Depends(require_admin)):
    data = await dashboard_metrics.fetch_panel_approval_sla()
    ctx = await _panel_ctx(request, user, "approval_sla", data)
    return templates.TemplateResponse("dashboard/partials/system_approval_sla.html", ctx)


@router.get("/dashboard/panel/slos", response_class=HTMLResponse)
async def panel_slos(request: Request, user: dict = Depends(require_admin)):
    data = await dashboard_metrics.fetch_panel_slos()
    ctx = await _panel_ctx(request, user, "slos", data)
    return templates.TemplateResponse("dashboard/partials/system_slos.html", ctx)


@router.get("/dashboard/panel/reviews", response_class=HTMLResponse)
async def panel_reviews(request: Request, user: dict = Depends(require_admin)):
    data = await dashboard_metrics.fetch_panel_reviews()
    ctx = await _panel_ctx(request, user, "review_quality", data)
    return templates.TemplateResponse("dashboard/partials/system_reviews.html", ctx)


@router.get("/dashboard/panel/taximeter", response_class=HTMLResponse)
async def panel_taximeter(
    request: Request,
    user: dict = Depends(require_admin),
    window_weeks: int = Query(default=12, ge=1, le=104),
    goal_id: str | None = Query(default=None),
    view: str = Query(default="people"),
    sort: str = Query(default="overall"),
    sort_dir: str = Query(default="desc"),
):
    """Re-runs the Taximeter view over a different lookback window, scoped
    to a single goal, switched between the People and Goals aggregations,
    and/or re-sorted by a different column — the preset buttons, goal
    picker, view toggle, and column headers in system_taximeter.html
    re-issue this same GET with new params, no full dashboard reload needed
    (same pattern as the agent registry's Templates/Instances toggle). A
    blank goal_id means "all goals"; an unrecognized view falls back to
    "people"; an unrecognized sort column falls back to "overall" (checked
    in compute_activity, not here, so this stays a thin passthrough)."""
    view = view if view in ("people", "goals") else "people"
    sort_dir = sort_dir if sort_dir in ("asc", "desc") else "desc"
    data = await dashboard_metrics.fetch_panel_taximeter(window_weeks, goal_id or None, view, sort, sort_dir)
    ctx = await _panel_ctx(request, user, "taximeter", data)
    return templates.TemplateResponse("dashboard/partials/system_taximeter.html", ctx)


@router.get("/dashboard/panel/taximeter/{uid}/goals", response_class=HTMLResponse)
async def panel_taximeter_goal_breakdown(
    uid: str,
    request: Request,
    user: dict = Depends(require_admin),
    window_weeks: int = Query(default=12, ge=1, le=104),
    name: str = Query(default=""),
    goal_id: str | None = Query(default=None),
):
    """Lazy-loaded into the per-person modal when a name is clicked in the
    main Taximeter table — one row per goal this person is active in, with
    that goal's own Oversight/Throughput/Efficiency/Overall scores. Narrows
    to just one row when `goal_id` is given, matching whatever the main
    table row itself is currently scoped to."""
    breakdown = await dashboard_metrics.fetch_taximeter_goal_breakdown(uid, window_weeks, goal_id or None)
    return templates.TemplateResponse(
        "dashboard/partials/_taximeter_goal_breakdown.html",
        {"request": request, "user": user, "breakdown": breakdown, "person_name": name or uid},
    )


@router.get("/dashboard/goal-cost", response_class=HTMLResponse)
async def goal_cost_panel(
    request: Request,
    user: dict = Depends(require_admin),
    goal_id: str = Query(default=""),
):
    """HTMX endpoint: renders the goal cost breakdown partial for a chosen goal.

    Also used for the initial render (goal_id="") which just shows the dropdown
    with no chart until the user picks a goal.
    """
    pool = get_pool()

    # Active goals for the dropdown, ordered by name.
    goal_rows = await pool.fetch(
        "SELECT id, name FROM goals WHERE status = 'active' ORDER BY name"
    )
    goals = [{"id": str(r["id"]), "name": r["name"]} for r in goal_rows]

    breakdown = None
    if goal_id:
        breakdown = await dashboard_metrics.goal_cost_breakdown(goal_id)

    _log.info("goal_cost_panel goal=%s goals=%d", goal_id or "(none)", len(goals))
    return templates.TemplateResponse(
        "dashboard/partials/goal_cost_breakdown.html",
        {
            "request":   request,
            "user":      user,
            "goals":     goals,
            "goal_id":   goal_id,
            "breakdown": breakdown,
        },
    )


# ── Task Queue: claude-code DAG concurrency ceiling (TASK_DAG_CEILING_PLAN.md) ──
# Lives as the "Task Queue" tab on the admin Dashboard (next to Taximeter). The
# tab lazy-loads the panel below, mirroring /dashboard/panel/taximeter.

@router.get("/dashboard/task-queue")
async def task_queue_redirect(user: dict = Depends(require_admin)):
    """Back-compat for the old standalone URL / bookmarks → the dashboard tab."""
    return RedirectResponse(url="/dashboard?tab=task_queue", status_code=303)


@router.get("/dashboard/panel/task-queue", response_class=HTMLResponse)
async def panel_task_queue(request: Request, user: dict = Depends(require_admin)):
    """Task Queue panel: executing (k/N) + waiting (FIFO) DAGs + the live N /
    enforce controls. Lazy-loaded by the dashboard tab (hx-trigger=load)."""
    from app.services import dag_scheduler  # noqa: PLC0415
    n, enforce = await dag_scheduler.get_ceiling_config()
    executing = await dag_scheduler.get_executing_dags()
    waiting = await dag_scheduler.get_waiting_dags()

    # Best-effort enrichment with story title + goal name (never fail the panel).
    try:
        pool = get_pool()
        rows = executing + waiting
        sids = list({d["story_id"] for d in rows if d.get("story_id")})
        gids = list({d["goal_id"] for d in rows if d.get("goal_id")})
        titles = {r["id"]: r["title"] for r in await pool.fetch(
            "SELECT id, title FROM stories WHERE id = ANY($1::text[])", sids)} if sids else {}
        names = {r["id"]: r["name"] for r in await pool.fetch(
            "SELECT id, name FROM goals WHERE id = ANY($1::text[])", gids)} if gids else {}
        for d in rows:
            d["story_title"] = titles.get(d.get("story_id"), "")
            d["goal_name"] = names.get(d.get("goal_id"), d.get("goal_id") or "")
    except Exception:
        _log.warning("task-queue enrichment failed (non-fatal)", exc_info=True)

    return templates.TemplateResponse(
        "dashboard/partials/system_task_queue.html",
        {"request": request, "user": user, "n": n, "enforce": enforce,
         "executing": executing, "waiting": waiting},
    )


@router.post("/dashboard/task-queue/config")
async def task_queue_config(
    request: Request,
    user: dict = Depends(require_admin),
    n: int = Form(...),
    enforce: str = Form(default=""),
):
    """Update the live ceiling N + enforce flag (admin only). Read each tick by
    the scheduler, so it takes effect within one tick — no redeploy."""
    n = max(1, int(n))
    enforce_bool = enforce.strip().lower() in ("on", "true", "1", "yes")
    await get_pool().execute(
        "UPDATE dag_ceiling_config SET n=$1, enforce=$2, updated_at=NOW(), updated_by=$3 "
        "WHERE id='singleton'",
        n, enforce_bool, (user.get("email") or user.get("uid") or "admin"),
    )
    _log.info("task-queue config updated: n=%d enforce=%s by=%s",
              n, enforce_bool, user.get("email"))
    return RedirectResponse(url="/dashboard?tab=task_queue", status_code=303)
