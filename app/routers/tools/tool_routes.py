"""Tool catalog routes — JSON API and admin UI for individual tools.

JSON API:
  GET   /api/v1/tools                — list tools
  GET   /api/v1/tools/{name}         — get tool
  POST  /api/v1/tools                — create tool
  PATCH /api/v1/tools/{name}         — update tool
  POST  /api/v1/tools/{name}/transition — lifecycle transition

HTML UI:
  GET  /tools                     — combined tool + profile list (two tabs)
  GET  /tools/new                 — register tool form
  POST /tools                     — register tool (form submit)
  GET  /tools/{name}              — edit tool form + usage panel
  POST /tools/{name}              — update tool (form submit)
  POST /tools/{name}/transition   — lifecycle transition (form submit)
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from asyncpg.exceptions import CheckViolationError

from app.auth import require_admin, get_current_user
from app.roles import SystemRole
from app.database import get_pool
from app.openclaw import gateway_admin
from app.models.tool import ToolCreateRequest, ToolUpdateRequest
from app.services.custom_tools.crud import count_custom_tools, list_custom_tools_page, list_tool_credentials
from app.services.tool_registry.catalog_sync import sync_openclaw_catalog
from app.services.tool_registry import (
    create_tool,
    get_tool,
    list_tools,
    transition_tool,
    update_tool,
)
from app.services.transitions.tools import (
    TOOL_ACTIVE_STATUSES,
    TOOL_TRANSITIONS,
    is_code_level_native_tool,
)

_templates = Jinja2Templates(directory="app/templates")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# 3-col card grid → 3 complete rows per page.
_TOOLS_PAGE_SIZE = 9
# 3-col card grid → 2 complete rows per page.
_CUSTOM_TOOLS_PAGE_SIZE = 6

router = APIRouter()
logger = logging.getLogger(__name__)


def _integrity_error_detail(exc: CheckViolationError) -> str:
    """A CheckViolationError backstop catches ANY tools-table CHECK, so map the specific
    constraint to an accurate message rather than always blaming source/tier."""
    return {
        "tools_source_tier_consistency": "source and tier are inconsistent (native tools have no tier; custom tools do).",
        "tools_source_check": "source must be 'core', 'custom', or 'platform'.",
        "tools_native_is_core": "a native tool (no tier) must be a System tool.",
        "tools_native_active_only": "a code-level native tool can only be 'enabled' or 'beta'.",
    }.get(getattr(exc, "constraint_name", None), "the tool violates a registry integrity constraint.")


# ── JSON API ───────────────────────────────────────────────────────────────

@router.get("/api/v1/tools")
async def api_list_tools(
    group: str = "",
    status: str = "enabled",
    _user: dict = Depends(get_current_user),
):
    """List all tools, optionally filtered by group_name and/or status."""
    pool = get_pool()
    tools = await list_tools(pool, group_name=group, status=status)
    logger.info("Listed %d tools (group=%r status=%s)", len(tools), group, status)
    return JSONResponse(jsonable_encoder({"tools": tools}))


@router.get("/api/v1/tools/{name}")
async def api_get_tool(name: str, _user: dict = Depends(get_current_user)):
    pool = get_pool()
    tool = await get_tool(pool, name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    logger.debug("Tool fetched via API: name=%s by %s", name, _user.get("email"))
    return JSONResponse(jsonable_encoder(tool))


@router.post("/api/v1/tools", status_code=201)
async def api_create_tool(req: ToolCreateRequest, _user: dict = Depends(require_admin)):
    pool = get_pool()
    try:
        tool = await create_tool(
            pool, req.name, req.label, req.description, req.group_name, req.tags, req.source,
        )
        logger.info("Tool created via API: name=%s source=%s by %s", req.name, req.source, _user.get("email"))
        return JSONResponse(jsonable_encoder({"ok": True, "tool": tool}), status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except CheckViolationError as exc:  # tools_* CHECK backstop
        logger.warning("Blocked inconsistent tool create via API: name=%s source=%s constraint=%s", req.name, req.source, getattr(exc, "constraint_name", None))
        raise HTTPException(status_code=400, detail=f"Invalid tool: {_integrity_error_detail(exc)}")


@router.patch("/api/v1/tools/{name}")
async def api_update_tool(
    name: str, patch: ToolUpdateRequest, _user: dict = Depends(require_admin),
):
    pool = get_pool()
    try:
        tool = await update_tool(
            pool, name,
            label=patch.label,
            description=patch.description,
            group_name=patch.group_name,
            tags=patch.tags,
            source=patch.source,
        )
        logger.info("Tool updated via API: name=%s by %s", name, _user.get("email"))
        return JSONResponse(jsonable_encoder({"ok": True, "tool": tool}))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CheckViolationError as exc:  # tools_* CHECK backstop (e.g. source→'custom' on a native tool)
        logger.warning("Blocked inconsistent tool update via API: name=%s constraint=%s", name, getattr(exc, "constraint_name", None))
        raise HTTPException(status_code=400, detail=f"Invalid update: {_integrity_error_detail(exc)}")


@router.post("/api/v1/tools/{name}/transition")
async def api_transition_tool(name: str, body: dict, _user: dict = Depends(require_admin)):
    """Transition a tool to a new lifecycle status.

    Body: {"status": "disabled"}
    Valid transitions are enforced by the TOOL_TRANSITIONS map in app/services/transitions/tools.py.
    """
    target_status = (body.get("status") or "").strip()
    if not target_status:
        raise HTTPException(status_code=400, detail="'status' is required")
    pool = get_pool()
    try:
        tool = await transition_tool(pool, name, target_status)
        logger.info("Tool transitioned via API: name=%s → %s by %s", name, target_status, _user.get("email"))
        return JSONResponse(jsonable_encoder({"ok": True, "tool": tool}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CheckViolationError:
        # DB-level backstop for tools_native_active_only, in case a protected native
        # tool is targeted through a path the app-layer guard didn't cover.
        logger.warning("Blocked native-tool knock-out via API: name=%s → %s", name, target_status)
        raise HTTPException(
            status_code=400,
            detail=f"Tool '{name}' is a code-level native tool and cannot be set to '{target_status}'.",
        )


@router.post("/api/tools/registry/sync")
async def api_sync_registry(_user: dict = Depends(require_admin)):
    """Sync the OpenClaw tool catalog into the tools table.

    Fetches the live catalog via tools.catalog RPC, upserts all catalog tools,
    and deprecates any catalog-sourced tools no longer in the catalog.
    Custom tools (tier IS NOT NULL) are never touched.
    """
    pool = get_pool()
    try:
        result = await sync_openclaw_catalog(pool, gateway_admin)
        logger.info(
            "Registry sync via API: added=%d updated=%d deprecated=%d by %s",
            result.get("added", 0), result.get("updated", 0), result.get("deprecated", 0),
            _user.get("email"),
        )
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        logger.warning("Registry sync API error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


# ── HTML UI ────────────────────────────────────────────────────────────────

@router.get("/settings/tools", response_class=HTMLResponse)
async def ui_tools_list(
    request: Request,
    tab: str = "system-tools",
    tools_page: int = 1,
    tools_q: str = "",
    profiles_page: int = 1,
    profiles_q: str = "",
    custom_tools_page: int = 1,
    custom_tools_q: str = "",
    custom_tools_tier: str = "",
    _user: dict = Depends(require_admin),
):
    pool = get_pool()
    active_statuses = list(TOOL_ACTIVE_STATUSES)

    # ── Tools pagination ────────────────────────────────────────────────────
    tools_offset = (tools_page - 1) * _TOOLS_PAGE_SIZE
    tools_search_pattern = f"%{tools_q}%" if tools_q else None
    if tools_q:
        tools_total = await pool.fetchval(
            """SELECT COUNT(*) FROM tools WHERE status = ANY($2::text[]) AND is_core = true
               AND (name ILIKE $1 OR label ILIKE $1 OR group_name ILIKE $1 OR source ILIKE $1)""",
            tools_search_pattern, active_statuses,
        )
        tools_rows = await pool.fetch(
            """SELECT * FROM tools WHERE status = ANY($4::text[]) AND is_core = true
               AND (name ILIKE $1 OR label ILIKE $1 OR group_name ILIKE $1 OR source ILIKE $1)
               ORDER BY group_name, name LIMIT $2 OFFSET $3""",
            tools_search_pattern, _TOOLS_PAGE_SIZE, tools_offset, active_statuses,
        )
    else:
        tools_total = await pool.fetchval(
            "SELECT COUNT(*) FROM tools WHERE status = ANY($1::text[]) AND is_core = true", active_statuses,
        )
        tools_rows = await pool.fetch(
            """SELECT * FROM tools WHERE status = ANY($3::text[]) AND is_core = true
               ORDER BY group_name, name LIMIT $1 OFFSET $2""",
            _TOOLS_PAGE_SIZE, tools_offset, active_statuses,
        )
    tools_pages = max(1, (tools_total + _TOOLS_PAGE_SIZE - 1) // _TOOLS_PAGE_SIZE)

    # ── Profiles pagination — both tabs share the /tools URL ────────────────
    from app.routers.tools.profile_routes import _PROFILES_PAGE_SIZE
    profiles_offset = (profiles_page - 1) * _PROFILES_PAGE_SIZE
    profiles_search_pattern = f"%{profiles_q}%" if profiles_q else None
    if profiles_q:
        profiles_total = await pool.fetchval(
            """SELECT COUNT(*) FROM tool_profiles WHERE status = 'active'
               AND (id ILIKE $1 OR name ILIKE $1 OR description ILIKE $1)""",
            profiles_search_pattern,
        )
        profiles_rows = await pool.fetch(
            """SELECT tp.id, tp.name, tp.description, tp.status, tp.created_at, tp.updated_at,
                      ARRAY_AGG(tpt.tool_name) FILTER (WHERE tpt.tool_name IS NOT NULL) AS tool_names
               FROM tool_profiles tp
               LEFT JOIN tool_profile_tools tpt ON tpt.profile_id = tp.id
               WHERE tp.status = 'active'
                 AND (tp.id ILIKE $1 OR tp.name ILIKE $1 OR tp.description ILIKE $1)
               GROUP BY tp.id
               ORDER BY tp.id
               LIMIT $2 OFFSET $3""",
            profiles_search_pattern, _PROFILES_PAGE_SIZE, profiles_offset,
        )
    else:
        profiles_total = await pool.fetchval(
            "SELECT COUNT(*) FROM tool_profiles WHERE status = 'active'",
        )
        profiles_rows = await pool.fetch(
            """SELECT tp.id, tp.name, tp.description, tp.status, tp.created_at, tp.updated_at,
                      ARRAY_AGG(tpt.tool_name) FILTER (WHERE tpt.tool_name IS NOT NULL) AS tool_names
               FROM tool_profiles tp
               LEFT JOIN tool_profile_tools tpt ON tpt.profile_id = tp.id
               WHERE tp.status = 'active'
               GROUP BY tp.id
               ORDER BY tp.id
               LIMIT $1 OFFSET $2""",
            _PROFILES_PAGE_SIZE, profiles_offset,
        )
    profiles_pages = max(1, (profiles_total + _PROFILES_PAGE_SIZE - 1) // _PROFILES_PAGE_SIZE)

    # Group paginated tools by group_name for the card layout.
    tools_list = [dict(r) for r in tools_rows]
    tools_by_group: dict[str, list[dict]] = {}
    for t in tools_list:
        tools_by_group.setdefault(t.get("group_name") or "other", []).append(t)

    # Fetch all non-active tools (disabled, deprecated, unavailable) for the collapsed section.
    inactive_rows = await pool.fetch(
        """SELECT * FROM tools WHERE status != ALL($1::text[]) AND is_core = true
           ORDER BY status, group_name, name""",
        active_statuses,
    )
    inactive_tools = [dict(r) for r in inactive_rows]

    # Custom tools — total count (for the tab badge) is unfiltered; the page
    # of rows shown respects the tab's own search/pagination/tier-filter state.
    custom_tools_total = await count_custom_tools(pool)
    custom_tools_matches = (
        await count_custom_tools(pool, custom_tools_q, custom_tools_tier)
        if custom_tools_q or custom_tools_tier else custom_tools_total
    )
    custom_tools_pages = max(1, (custom_tools_matches + _CUSTOM_TOOLS_PAGE_SIZE - 1) // _CUSTOM_TOOLS_PAGE_SIZE)
    custom_tools_offset = (custom_tools_page - 1) * _CUSTOM_TOOLS_PAGE_SIZE
    custom_tools = await list_custom_tools_page(
        pool, custom_tools_q, _CUSTOM_TOOLS_PAGE_SIZE, custom_tools_offset, custom_tools_tier,
    )
    credentials = await list_tool_credentials(pool)

    valid_tabs = ("system-tools", "profiles", "custom-tools")
    logger.info(
        "Tool Registry list viewed by %s: tab=%s tools(q=%r page=%d/%d) "
        "profiles(q=%r page=%d/%d) custom_tools(q=%r page=%d/%d)",
        _user.get("email"), tab, tools_q, tools_page, tools_pages,
        profiles_q, profiles_page, profiles_pages,
        custom_tools_q, custom_tools_page, custom_tools_pages,
    )
    return _templates.TemplateResponse("tools/list.html", {
        "request": request,
        "user": _user,
        "tab": tab if tab in valid_tabs else "system-tools",
        # tools (card view)
        "tools": tools_list,
        "tools_by_group": tools_by_group,
        "tools_total": tools_total,
        "tools_page": tools_page,
        "tools_pages": tools_pages,
        "tools_q": tools_q,
        # profiles (tile view)
        "profiles": [dict(r) for r in profiles_rows],
        "profiles_total": profiles_total,
        "profiles_page": profiles_page,
        "profiles_pages": profiles_pages,
        "profiles_q": profiles_q,
        # inactive tools (disabled/deprecated/unavailable) — collapsed section
        "inactive_tools": inactive_tools,
        # custom tools tab (flat tile list, optionally filtered to one tier)
        "custom_tools": custom_tools,
        "custom_tools_total": custom_tools_total,
        "custom_tools_matches": custom_tools_matches,
        "custom_tools_page": custom_tools_page,
        "custom_tools_pages": custom_tools_pages,
        "custom_tools_q": custom_tools_q,
        "custom_tools_tier": custom_tools_tier,
        "credentials": credentials,
        "can_edit": _user.get("system_role") == SystemRole.PLATFORM_ADMIN,
    })


@router.get("/settings/tools/new", response_class=HTMLResponse)
async def ui_tool_new(request: Request, _user: dict = Depends(require_admin)):
    return _templates.TemplateResponse("tools/detail.html", {
        "request": request,
        "user": _user,
        "tool": None,
        "is_new": True,
        "profiles_for_tool": [],
        "can_edit": True,
    })


@router.post("/settings/tools")
async def ui_tool_create(request: Request, _user: dict = Depends(require_admin)):
    pool = get_pool()
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name or not TOOL_NAME_RE.match(name):
        return _templates.TemplateResponse("tools/detail.html", {
            "request": request,
            "user": _user,
            "tool": dict(form),
            "is_new": True,
            "profiles_for_tool": [],
            "can_edit": True,
            "error": "Name must be lowercase letters, digits, underscores or hyphens, starting with a letter.",
        }, status_code=422)

    tags_raw = (form.get("tags") or "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

    try:
        await create_tool(
            pool,
            name=name,
            label=(form.get("label") or "").strip(),
            description=(form.get("description") or "").strip(),
            group_name=(form.get("group_name") or "").strip(),
            tags=tags,
            source=(form.get("source") or "core").strip(),
        )
        logger.info("Tool created via UI: name=%s by %s", name, _user.get("email"))
    except (ValueError, CheckViolationError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else f"Invalid tool: {_integrity_error_detail(exc)}"
        return _templates.TemplateResponse("tools/detail.html", {
            "request": request,
            "user": _user,
            "tool": dict(form),
            "is_new": True,
            "profiles_for_tool": [],
            "can_edit": True,
            "error": error,
        }, status_code=409)

    return RedirectResponse(url=f"/settings/tools/{name}", status_code=303)


@router.get("/settings/tools/{name}", response_class=HTMLResponse)
async def ui_tool_detail(name: str, request: Request, _user: dict = Depends(get_current_user)):
    pool = get_pool()
    tool = await get_tool(pool, name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    if tool["tier"] is not None:
        # Custom-tier tools (even is_core ones) always edit through their
        # tier-specific form — this generic page is native-tools-only.
        return RedirectResponse(url=f"/custom-tools/{name}")
    valid_transitions = sorted(TOOL_TRANSITIONS.get(tool["status"], set()))
    # Code-level native tools may only ever be active — don't advertise knock-out
    # transitions (disabled/deprecated/unavailable) the DB constraint would reject.
    if is_code_level_native_tool(tool):
        valid_transitions = [s for s in valid_transitions if s in TOOL_ACTIVE_STATUSES]

    # Fetch profiles that include this tool.
    profile_rows = await pool.fetch(
        """SELECT tp.id, tp.name
           FROM tool_profile_tools tpt
           JOIN tool_profiles tp ON tp.id = tpt.profile_id
           WHERE tpt.tool_name = $1
           ORDER BY tp.id""",
        name,
    )
    profile_ids = [r["id"] for r in profile_rows]
    profiles_for_tool = [dict(r) for r in profile_rows]

    # For each profile, fetch which templates use it — single query for all profiles.
    if profile_ids:
        template_rows = await pool.fetch(
            """SELECT ttp.profile_id, t.id AS template_id, t.name AS template_name, t.type AS template_type
               FROM template_tool_profiles ttp
               JOIN templates t ON t.id = ttp.template_id
               WHERE ttp.profile_id = ANY($1::text[])
               ORDER BY ttp.profile_id, t.type""",
            profile_ids,
        )
        templates_by_profile: dict[str, list[dict]] = {pid: [] for pid in profile_ids}
        for r in template_rows:
            templates_by_profile[r["profile_id"]].append({
                "id": r["template_id"],
                "name": r["template_name"],
                "type": r["template_type"],
            })
        for p in profiles_for_tool:
            p["templates"] = templates_by_profile.get(p["id"], [])
    else:
        for p in profiles_for_tool:
            p["templates"] = []

    logger.debug("Tool detail: name=%s profiles=%d", name, len(profiles_for_tool))
    return _templates.TemplateResponse("tools/detail.html", {
        "request": request,
        "user": _user,
        "tool": tool,
        "is_new": False,
        "valid_transitions": valid_transitions,
        "profiles_for_tool": profiles_for_tool,
        "can_edit": _user.get("system_role") == SystemRole.PLATFORM_ADMIN,
    })


@router.post("/settings/tools/{name}")
async def ui_tool_update(name: str, request: Request, _user: dict = Depends(require_admin)):
    pool = get_pool()
    form = await request.form()
    tags_raw = (form.get("tags") or "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    try:
        await update_tool(
            pool, name,
            label=(form.get("label") or "").strip() or None,
            description=(form.get("description") or "").strip() or None,
            group_name=(form.get("group_name") or "").strip() or None,
            tags=tags,
            source=(form.get("source") or "").strip() or None,
        )
        logger.info("Tool updated via UI: name=%s by %s", name, _user.get("email"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CheckViolationError as exc:  # tools_* CHECK backstop
        logger.warning("Blocked inconsistent tool update via UI: name=%s constraint=%s", name, getattr(exc, "constraint_name", None))
        raise HTTPException(status_code=400, detail=f"Invalid update: {_integrity_error_detail(exc)}")
    return RedirectResponse(url=f"/settings/tools/{name}", status_code=303)


@router.post("/settings/tools/{name}/transition")
async def ui_tool_transition(name: str, request: Request, _user: dict = Depends(require_admin)):
    form = await request.form()
    target_status = (form.get("target_status") or "").strip()
    if not target_status:
        raise HTTPException(status_code=400, detail="target_status is required")
    pool = get_pool()
    try:
        await transition_tool(pool, name, target_status)
        logger.info("Tool transitioned via UI: name=%s → %s by %s", name, target_status, _user.get("email"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CheckViolationError:
        logger.warning("Blocked native-tool knock-out via UI: name=%s → %s", name, target_status)
        raise HTTPException(
            status_code=400,
            detail=f"Tool '{name}' is a code-level native tool and cannot be set to '{target_status}'.",
        )
    return RedirectResponse(url=f"/settings/tools/{name}", status_code=303)
