"""Admin UI routes for custom tools — rendered Jinja2 pages, admin-only."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin, get_current_user
from app.roles import SystemRole
from app.database import get_pool
from app.services.custom_tools.crud import (
    get_custom_tool,
    list_custom_tools,
    list_tool_credentials,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/custom-tools")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def custom_tools_list(request: Request, user=Depends(get_current_user)):
    pool = get_pool()
    tools = await list_custom_tools(pool)
    return templates.TemplateResponse(
        "custom_tools/list.html",
        {"request": request, "user": user, "tools": tools,
         "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN},
    )


# ── Credentials (registered before /{tool_id} to avoid shadowing) ─────────────

@router.get("/credentials", response_class=HTMLResponse)
async def credentials_list(request: Request, user=Depends(get_current_user)):
    """Viewable by anyone — only credential names/types/rotation metadata are
    shown, never secret values (those are Fernet-encrypted, write-only)."""
    pool = get_pool()
    credentials = await list_tool_credentials(pool)
    return templates.TemplateResponse(
        "custom_tools/credentials.html",
        {"request": request, "user": user, "credentials": credentials,
         "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN},
    )


@router.get("/credentials/new", response_class=HTMLResponse)
async def credentials_new(request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse(
        "custom_tools/credential_form.html",
        {"request": request, "user": user, "credential": None},
    )


# ── OpenAPI Import (registered before /{tool_id}) ─────────────────────────────

@router.get("/openapi/import", response_class=HTMLResponse)
async def openapi_import(request: Request, user=Depends(require_admin)):
    pool = get_pool()
    credentials = await list_tool_credentials(pool)
    return templates.TemplateResponse(
        "custom_tools/openapi_import.html",
        {"request": request, "user": user, "credentials": credentials},
    )


# ── New tool form (registered before /{tool_id}) ──────────────────────────────

@router.get("/mcp/import", response_class=HTMLResponse)
async def mcp_import(request: Request, user=Depends(require_admin)):
    pool = get_pool()
    credentials = await list_tool_credentials(pool)
    return templates.TemplateResponse(
        "custom_tools/mcp_import.html",
        {"request": request, "user": user, "credentials": credentials},
    )


@router.get("/new/{tier}", response_class=HTMLResponse)
async def custom_tool_new(request: Request, tier: str, user=Depends(require_admin)):
    if tier not in ("http_action", "typescript", "openapi"):
        raise HTTPException(status_code=404, detail="unknown tier")
    pool = get_pool()
    credentials = await list_tool_credentials(pool)
    return templates.TemplateResponse(
        f"custom_tools/{tier}_form.html",
        {
            "request": request,
            "user": user,
            "credentials": credentials,
            # JSON-safe subset for the typescript capability-builder JS injection.
            # Jinja's `tojson` filter chokes on datetime fields in the full list
            # and doesn't support dict literals inside list comprehensions, so we
            # pre-filter to just the two fields the JS dropdown reads.
            "credentials_for_js": [
                {"name": c["name"], "credential_type": c["credential_type"]}
                for c in credentials
            ],
            "tool": None,
        },
    )


# ── Tool detail/edit (parameterised — must come last) ─────────────────────────

@router.get("/{tool_name}", response_class=HTMLResponse)
async def custom_tool_detail(request: Request, tool_name: str, user=Depends(get_current_user)):
    """Viewable by anyone logged in. `can_edit` (admin-only) tells each
    tier-specific template whether to render its editable form or a read-only
    dump of the same config — see custom_tools/*_form.html."""
    pool = get_pool()
    tool = await get_custom_tool(pool, tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    credentials = await list_tool_credentials(pool)
    tier = tool["tier"]
    # mcp_tool and mcp_server tiers share the mcp form (read-only view for discovered tools).
    template_name = "custom_tools/mcp_tool_detail.html" if tier in ("mcp_tool", "mcp_server") else f"custom_tools/{tier}_form.html"
    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "user": user,
            "tool": tool,
            "credentials": credentials,
            # JSON-safe subset for the typescript capability-builder JS injection
            # (only consumed by typescript_form.html, harmless for the others).
            "credentials_for_js": [
                {"name": c["name"], "credential_type": c["credential_type"]}
                for c in credentials
            ],
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
        },
    )
