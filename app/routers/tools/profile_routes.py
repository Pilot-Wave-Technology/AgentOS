"""Tool profile routes — JSON API and admin UI for profile CRUD and template assignments.

JSON API:
  GET   /api/v1/tool-profiles              — list profiles
  GET   /api/v1/tool-profiles/{id}         — get profile
  POST  /api/v1/tool-profiles              — create profile
  PATCH /api/v1/tool-profiles/{id}         — update profile
  POST  /api/v1/tool-profiles/{id}/deprecate — deprecate profile
  POST  /api/v1/tool-profiles/resolve      — preview flat tool allowlist for given profiles

HTML UI:
  GET  /tool-profiles           — redirect to /tools (profiles tab is there)
  GET  /tool-profiles/new       — create profile form
  POST /tool-profiles           — create profile (form submit)
  GET  /tool-profiles/{id}      — edit profile form + template assignments section
  POST /tool-profiles/{id}      — update profile (form submit)
  POST /tool-profiles/{id}/templates  — save template assignments (form submit)
  POST /tool-profiles/{id}/deprecate  — deprecate profile (form submit)
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin, get_current_user
from app.roles import SystemRole
from app.database import get_pool
from app.models.tool import ToolProfileCreateRequest, ToolProfileUpdateRequest
from app.services.tool_registry import (
    create_profile,
    deprecate_profile,
    get_profile,
    get_profile_templates,
    list_profiles,
    resolve_tool_allowlist,
    set_profile_templates,
    update_profile,
)
from app.services.transitions.tools import TOOL_ACTIVE_STATUSES

_templates = Jinja2Templates(directory="app/templates")
PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# 3-col card grid → 2 complete rows per page.
_PROFILES_PAGE_SIZE = 6

router = APIRouter()
logger = logging.getLogger(__name__)


async def _tools_by_group(pool) -> dict[str, list[dict]]:
    """Fetch all active (enabled + beta) tools grouped by group_name for profile checkbox rendering."""
    rows = await pool.fetch(
        "SELECT * FROM tools WHERE status = ANY($1::text[]) ORDER BY group_name, name",
        list(TOOL_ACTIVE_STATUSES),
    )
    groups: dict[str, list[dict]] = {}
    for t in rows:
        groups.setdefault(t["group_name"] or "other", []).append(dict(t))
    return groups


# ── JSON API ───────────────────────────────────────────────────────────────

@router.get("/api/v1/tool-profiles")
async def api_list_profiles(
    status: str = "active",
    _user: dict = Depends(get_current_user),
):
    """List all tool profiles with their tool lists populated."""
    pool = get_pool()
    profiles = await list_profiles(pool, status=status)
    logger.info("Listed %d tool profiles (status=%s)", len(profiles), status)
    return JSONResponse(jsonable_encoder({"profiles": profiles}))


@router.post("/api/v1/tool-profiles/resolve")
async def api_resolve_profiles(
    body: dict,
    _user: dict = Depends(get_current_user),
):
    """Preview the flat tool allowlist that a set of profiles + direct tools would produce.

    Body: {"profiles": ["story-orchestrator"], "direct_tools": ["image"]}
    Response: {"allow": [...], "deny": [...]}

    Useful for the admin UI to preview what an agent type would receive before assigning profiles.
    """
    pool = get_pool()
    profiles = body.get("profiles") or []
    direct_tools = body.get("direct_tools") or []
    deny_tools = body.get("deny_tools") or []
    allow, deny = await resolve_tool_allowlist(pool, profiles, direct_tools, deny_tools)
    logger.info(
        "Resolved tool lists preview: profiles=%s direct=%d deny_in=%d → allow=%d deny=%d",
        profiles, len(direct_tools), len(deny_tools), len(allow), len(deny),
    )
    return JSONResponse({"allow": allow, "deny": deny})


@router.get("/api/v1/tool-profiles/{profile_id}")
async def api_get_profile(profile_id: str, _user: dict = Depends(get_current_user)):
    pool = get_pool()
    profile = await get_profile(pool, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    logger.debug("Profile fetched via API: id=%s by %s", profile_id, _user.get("email"))
    return JSONResponse(jsonable_encoder(profile))


@router.post("/api/v1/tool-profiles", status_code=201)
async def api_create_profile(
    req: ToolProfileCreateRequest, _user: dict = Depends(require_admin),
):
    pool = get_pool()
    try:
        profile = await create_profile(
            pool, req.id, req.name, req.description, req.tool_names,
        )
        logger.info("Tool profile created via API: id=%s tools=%d by %s", req.id, len(req.tool_names), _user.get("email"))
        return JSONResponse(jsonable_encoder({"ok": True, "profile": profile}), status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.patch("/api/v1/tool-profiles/{profile_id}")
async def api_update_profile(
    profile_id: str,
    patch: ToolProfileUpdateRequest,
    _user: dict = Depends(require_admin),
):
    pool = get_pool()
    try:
        profile = await update_profile(
            pool, profile_id,
            name=patch.name,
            description=patch.description,
            tool_names=patch.tool_names,
        )
        logger.info("Profile updated via API: id=%s by %s", profile_id, _user.get("email"))
        return JSONResponse(jsonable_encoder({"ok": True, "profile": profile}))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/v1/tool-profiles/{profile_id}/deprecate")
async def api_deprecate_profile(profile_id: str, _user: dict = Depends(require_admin)):
    pool = get_pool()
    try:
        profile = await deprecate_profile(pool, profile_id)
        logger.info("Tool profile deprecated via API: id=%s by %s", profile_id, _user.get("email"))
        return JSONResponse(jsonable_encoder({"ok": True, "profile": profile}))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── HTML UI ────────────────────────────────────────────────────────────────

@router.get("/settings/tool-profiles", response_class=HTMLResponse)
async def ui_profiles_list(_user: dict = Depends(get_current_user)):
    # Profile list is part of the combined Tool Registry page at /settings/tools.
    return RedirectResponse(url="/settings/tools?tab=profiles", status_code=302)


@router.get("/settings/tool-profiles/new", response_class=HTMLResponse)
async def ui_profile_new(request: Request, _user: dict = Depends(require_admin)):
    pool = get_pool()
    return _templates.TemplateResponse("tool_profiles/detail.html", {
        "request": request,
        "user": _user,
        "profile": None,
        "is_new": True,
        "tools_by_group": await _tools_by_group(pool),
        "selected_tools": set(),
        "all_templates": [],
        "assigned_template_ids": set(),
        "can_edit": True,
    })


@router.post("/settings/tool-profiles")
async def ui_profile_create(request: Request, _user: dict = Depends(require_admin)):
    pool = get_pool()
    form = await request.form()
    profile_id = (form.get("id") or "").strip()
    if not profile_id or not PROFILE_ID_RE.match(profile_id):
        return _templates.TemplateResponse("tool_profiles/detail.html", {
            "request": request,
            "user": _user,
            "profile": dict(form),
            "is_new": True,
            "tools_by_group": await _tools_by_group(pool),
            "selected_tools": set(form.getlist("tool_names")),
            "all_templates": [],
            "assigned_template_ids": set(),
            "can_edit": True,
            "error": "ID must be lowercase letters, digits, and hyphens, starting with a letter.",
        }, status_code=422)

    tool_names = form.getlist("tool_names")
    try:
        await create_profile(
            pool,
            profile_id=profile_id,
            name=(form.get("name") or "").strip(),
            description=(form.get("description") or "").strip(),
            tool_names=tool_names,
        )
        logger.info(
            "Tool profile created via UI: id=%s tools=%d by %s",
            profile_id, len(tool_names), _user.get("email"),
        )
    except ValueError as exc:
        return _templates.TemplateResponse("tool_profiles/detail.html", {
            "request": request,
            "user": _user,
            "profile": dict(form),
            "is_new": True,
            "tools_by_group": await _tools_by_group(pool),
            "selected_tools": set(tool_names),
            "all_templates": [],
            "assigned_template_ids": set(),
            "can_edit": True,
            "error": str(exc),
        }, status_code=409)

    return RedirectResponse(url=f"/settings/tool-profiles/{profile_id}", status_code=303)


@router.get("/settings/tool-profiles/{profile_id}", response_class=HTMLResponse)
async def ui_profile_detail(
    profile_id: str, request: Request, _user: dict = Depends(get_current_user),
):
    pool = get_pool()
    profile = await get_profile(pool, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    # Fetch all templates for the assignment section, and which are currently assigned.
    # The `type` column holds legacy DB values (e.g. 'SHCA'); the template renders
    # the user-facing label ('Assistant') inline, matching agent_registry/index.html.
    all_template_rows = await pool.fetch(
        "SELECT id, name, type FROM templates ORDER BY type, name",
    )
    all_templates = [dict(r) for r in all_template_rows]
    assigned_template_ids = set(await get_profile_templates(pool, profile_id))

    logger.debug(
        "Profile detail: id=%s assigned_templates=%d / %d total",
        profile_id, len(assigned_template_ids), len(all_templates),
    )
    return _templates.TemplateResponse("tool_profiles/detail.html", {
        "request": request,
        "user": _user,
        "profile": profile,
        "is_new": False,
        "tools_by_group": await _tools_by_group(pool),
        "selected_tools": set(profile.get("tool_names") or []),
        "all_templates": all_templates,
        "assigned_template_ids": assigned_template_ids,
        "can_edit": _user.get("system_role") == SystemRole.PLATFORM_ADMIN,
    })


@router.post("/settings/tool-profiles/{profile_id}")
async def ui_profile_update(
    profile_id: str, request: Request, _user: dict = Depends(require_admin),
):
    pool = get_pool()
    form = await request.form()
    tool_names = form.getlist("tool_names")
    try:
        await update_profile(
            pool, profile_id,
            name=(form.get("name") or "").strip() or None,
            description=(form.get("description") or "").strip() or None,
            tool_names=tool_names,
        )
        logger.info(
            "Tool profile updated via UI: id=%s tools=%d by %s",
            profile_id, len(tool_names), _user.get("email"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return RedirectResponse(url=f"/settings/tool-profiles/{profile_id}", status_code=303)


@router.post("/settings/tool-profiles/{profile_id}/templates")
async def ui_profile_set_templates(
    profile_id: str, request: Request, _user: dict = Depends(require_admin),
):
    """Save the set of templates this profile is assigned to."""
    pool = get_pool()
    form = await request.form()
    template_ids = form.getlist("template_ids")
    # An empty template_ids list is valid — it means "clear all assignments".
    logger.info(
        "Saving profile template assignments: profile=%s count=%d templates=%s by %s",
        profile_id, len(template_ids), template_ids, _user.get("email"),
    )
    try:
        await set_profile_templates(pool, profile_id, template_ids)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        # Any other failure would otherwise reach the client as an opaque
        # "Failed to save" toast with no server-side breadcrumb — log the full
        # traceback with the exact inputs so the cause is diagnosable.
        logger.exception(
            "Unexpected failure saving template assignments: profile=%s templates=%s",
            profile_id, template_ids,
        )
        raise
    return RedirectResponse(url=f"/settings/tool-profiles/{profile_id}", status_code=303)


@router.post("/settings/tool-profiles/{profile_id}/deprecate")
async def ui_profile_deprecate(
    profile_id: str, request: Request, _user: dict = Depends(require_admin),
):
    pool = get_pool()
    try:
        await deprecate_profile(pool, profile_id)
        logger.info("Tool profile deprecated via UI: id=%s by %s", profile_id, _user.get("email"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return RedirectResponse(url=f"/settings/tool-profiles/{profile_id}", status_code=303)
