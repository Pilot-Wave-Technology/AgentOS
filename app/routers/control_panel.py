"""Settings — single hub for all configuration and admin pages."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.services.user_preferences import set_preference
from app.services.tour_progress import reset_all_progress, mark_journey_abandoned

_log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_index(request: Request, user: dict = Depends(get_current_user)):
    _log.info("settings: user=%s role=%s", user["uid"], user.get("system_role"))
    return templates.TemplateResponse(
        "settings/index.html",
        {"request": request, "user": user},
    )


@router.get("/settings/preferences", response_class=HTMLResponse)
async def settings_preferences(request: Request, user: dict = Depends(get_current_user)):
    _log.info("settings/preferences: user=%s", user["uid"])
    return templates.TemplateResponse(
        "settings/preferences.html",
        {"request": request, "user": user},
    )


@router.post("/settings/preferences/{key}")
async def settings_preferences_toggle(
    key: str, request: Request, user: dict = Depends(get_current_user),
):
    form = await request.form()
    value = form.get("value") == "true"
    _log.info("settings/preferences: user=%s setting %s=%s", user["uid"], key, value)
    try:
        prefs = await set_preference(user["uid"], key, value)
    except ValueError:
        _log.warning("settings/preferences: unknown key=%s from user=%s", key, user["uid"])
        raise HTTPException(status_code=400, detail="Unknown preference")
    return prefs


@router.post("/settings/tour-progress/reset")
async def reset_tour_progress(user: dict = Depends(get_current_user)):
    await reset_all_progress(user["uid"])
    return {"ok": True}


@router.post("/settings/tour-progress/skip-remaining")
async def skip_remaining_tour_progress(user: dict = Depends(get_current_user)):
    """Closing a journey tour early (✕/Escape) cascades: mark every
    remaining milestone in the journey done so none of them auto-fire
    later. See abandonTour() in static/js/tour.js."""
    await mark_journey_abandoned(user["uid"])
    return {"ok": True}


# Redirect old URL so bookmarks don't break
@router.get("/control-panel", response_class=HTMLResponse)
async def control_panel_redirect(request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/settings", status_code=301)
