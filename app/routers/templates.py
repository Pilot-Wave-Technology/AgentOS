"""Templates registry — admin UI + JSON API.

The `templates` table is the source of truth for agent template
definitions (GO, SHCA, IA, RA). Each template owns workspace files in
`template_files` (AGENTS.md, SOUL.md, USER.md stub, blank IDENTITY.md).

Edits via the UI immediately fan out to every live instance of the
template via `template_sync.schedule_file_push` — the workspace
AGENTS.md / SOUL.md / etc. on live agents picks up the new content on
its next inference turn (no agent restart needed).
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, require_admin
from app.database import get_pool
from app.roles import SystemRole
from app.services.template_sync import (
    schedule_file_push,
    schedule_file_delete,
)
from app.services.tool_registry import get_template_profiles, list_profiles, set_template_profiles


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

ALLOWED_AGENT_TYPES = ("SHCA", "IA", "GO", "RA")

# Filenames editable via the template_files UI. The first four are the
# agent's main system-prompt surface. IDENTITY.md and HEARTBEAT.md /
# BOOTSTRAP.md are managed at the instance level so we don't surface
# them in the template editor.
EDITABLE_FILENAMES = ("AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md")


def _record_history(
    conn,
    template_id: str,
    version: int,
    snapshot: dict,
    changed_by: str,
    reason: str,
):
    return conn.execute(
        """
        INSERT INTO template_history
            (template_id, version, snapshot, changed_by, change_reason)
        VALUES ($1, $2, $3::jsonb, $4, $5)
        ON CONFLICT (template_id, version) DO NOTHING
        """,
        template_id, version, json.dumps(snapshot), changed_by, reason,
    )


# -- List view (redirects to consolidated Agent Registry) ---------------------

@router.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request, user: dict = Depends(get_current_user)):
    logger.debug("templates_list: redirecting to /settings/agent-registry")
    return RedirectResponse(url="/settings/agent-registry?view=templates", status_code=302)


# -- View / edit existing template --------------------------------------------
# Viewable by anyone logged in; can_edit (admin-only) gates the mutating
# forms rendered by templates/detail.html.

@router.get("/templates/{template_id}", response_class=HTMLResponse)
async def template_detail(
    template_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Local import: template_authoring.py imports _record_history from this
    # module at load time, so importing it back at module level here would
    # be circular.
    from app.routers.template_authoring import _model_options

    pool = get_pool()
    tmpl = await pool.fetchrow(
        "SELECT * FROM templates WHERE id = $1", template_id,
    )
    if not tmpl:
        raise HTTPException(status_code=404, detail="template not found")

    files = await pool.fetch(
        "SELECT filename, content, updated_at FROM template_files "
        "WHERE template_id = $1 ORDER BY filename",
        template_id,
    )
    files_by_name = {r["filename"]: dict(r) for r in files}

    instance_rows = await pool.fetch(
        """
        SELECT id, name, gateway_agent_id, status, created_at
          FROM agents
         WHERE template_id = $1
           AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = agents.id)
         ORDER BY created_at DESC
         LIMIT 50
        """,
        template_id,
    )

    applicable_skills = await pool.fetch(
        """
        SELECT name, title, status FROM skills
         WHERE $1 = ANY(applicable_to)
         ORDER BY status DESC, name
        """,
        tmpl["type"],
    )

    all_profiles = await list_profiles(pool, status="active")
    assigned_profile_ids = set(await get_template_profiles(pool, template_id))

    models = await _model_options()

    all_tools = await pool.fetch(
        "SELECT name, label, group_name, source FROM tools WHERE status = 'active' ORDER BY group_name, name",
    )
    tools_by_group: dict[str, list[dict]] = {}
    for t in all_tools:
        tools_by_group.setdefault(t["group_name"] or "other", []).append(dict(t))

    tmpl_tools_allow = list(tmpl.get("tools_allow") or [])
    tmpl_tools_deny = list(tmpl.get("tools_deny") or [])

    # Keep the template's current model selectable even if it's since fallen
    # off the allowlist (e.g. a deprecated model), so saving other fields
    # never silently changes it.
    if tmpl["model"] and tmpl["model"] not in models:
        models = [tmpl["model"], *models]

    return templates.TemplateResponse(
        "templates/detail.html",
        {
            "request": request,
            "user": user,
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
            "template": dict(tmpl),
            "models": models,
            "files_by_name": files_by_name,
            "editable_filenames": EDITABLE_FILENAMES,
            "instances": [dict(r) for r in instance_rows],
            "applicable_skills": [dict(r) for r in applicable_skills],
            "agent_types": ALLOWED_AGENT_TYPES,
            "all_profiles": all_profiles,
            "assigned_profile_ids": assigned_profile_ids,
            "tools_by_group": tools_by_group,
            "tmpl_tools_allow": set(tmpl_tools_allow),
            "tmpl_tools_deny": set(tmpl_tools_deny),
        },
    )


@router.post("/templates/{template_id}")
async def template_update(
    template_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Update template metadata (name, description, model, emoji, etc.).
    Admin-only: platform.editor has read-only access to the Agent Registry.
    Does NOT update template_files — those have their own endpoint so
    each file's push-on-write fan-out can be scoped to that file.
    """
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    model = (form.get("model") or "").strip()
    emoji = (form.get("emoji") or "").strip()
    notes = (form.get("notes") or "").strip()
    change_reason = (form.get("change_reason") or "edited via admin UI").strip()

    if not name or not model:
        raise HTTPException(status_code=400, detail="name and model are required")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM templates WHERE id = $1 FOR UPDATE", template_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="template not found")

            unchanged = (
                row["name"] == name
                and row["description"] == description
                and row["model"] == model
                and row["emoji"] == emoji
                and row["notes"] == notes
            )
            if unchanged:
                return RedirectResponse(url=f"/templates/{template_id}", status_code=303)

            new_version = row["version"] + 1
            await _record_history(
                conn, template_id, row["version"],
                {
                    "name": row["name"],
                    "description": row["description"],
                    "model": row["model"],
                    "emoji": row["emoji"],
                    "notes": row["notes"],
                },
                user.get("uid", "unknown"),
                change_reason,
            )
            await conn.execute(
                """
                UPDATE templates
                   SET name = $2, description = $3, model = $4,
                       emoji = $5, notes = $6, version = $7, updated_at = NOW()
                 WHERE id = $1
                """,
                template_id, name, description, model, emoji, notes, new_version,
            )

    return RedirectResponse(url=f"/templates/{template_id}", status_code=303)


# -- Per-template file edit (the heavy lifter) -------------------------------

@router.post("/templates/{template_id}/files/{filename}")
async def template_file_update(
    template_id: str,
    filename: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Replace a template file's content. Fans out to every live instance.
    Admin-only: platform.editor has read-only access to the Agent Registry."""
    if filename not in EDITABLE_FILENAMES:
        raise HTTPException(
            status_code=400,
            detail=f"filename {filename!r} is not in the editable list",
        )

    form = await request.form()
    content = form.get("content") or ""
    change_reason = (form.get("change_reason") or "file edited via admin UI").strip()

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            tmpl = await conn.fetchrow(
                "SELECT version FROM templates WHERE id = $1 FOR UPDATE",
                template_id,
            )
            if not tmpl:
                raise HTTPException(status_code=404, detail="template not found")

            existing = await conn.fetchval(
                "SELECT content FROM template_files "
                "WHERE template_id = $1 AND filename = $2",
                template_id, filename,
            )
            if existing == content:
                return RedirectResponse(
                    url=f"/templates/{template_id}", status_code=303,
                )

            new_version = tmpl["version"] + 1
            await _record_history(
                conn, template_id, tmpl["version"],
                {
                    "filename": filename,
                    "prior_content_excerpt": (existing or "")[:500],
                },
                user.get("uid", "unknown"),
                f"{change_reason} (file={filename})",
            )
            await conn.execute(
                """
                INSERT INTO template_files
                    (template_id, filename, content, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (template_id, filename)
                DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
                """,
                template_id, filename, content,
            )
            await conn.execute(
                "UPDATE templates SET version = $2, updated_at = NOW() WHERE id = $1",
                template_id, new_version,
            )

    # Fan-out: push the new file to every live instance of this template.
    # Async — UI redirect returns immediately, errors are logged.
    schedule_file_push(template_id, filename, content)

    return RedirectResponse(url=f"/templates/{template_id}", status_code=303)


# -- JSON API ----------------------------------------------------------------

@router.post("/templates/{template_id}/tools")
async def template_tools_update(
    template_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Update the template's direct tools_allow and tools_deny lists.
    Admin-only: platform.editor has read-only access to the Agent Registry.

    These are per-template overrides applied on top of assigned profiles during
    resolution. tools_allow adds tools regardless of profiles; tools_deny removes
    tools even if a profile grants them (subject to cancellation in the resolver).
    """
    pool = get_pool()
    tmpl = await pool.fetchrow("SELECT id, version FROM templates WHERE id = $1", template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="template not found")

    form = await request.form()
    tools_allow = form.getlist("tools_allow")
    tools_deny = form.getlist("tools_deny")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE templates SET tools_allow = $2, tools_deny = $3, updated_at = NOW()
                   WHERE id = $1""",
                template_id, tools_allow, tools_deny,
            )
    logger.info(
        "Template %s direct tools updated: allow=%d deny=%d by %s",
        template_id, len(tools_allow), len(tools_deny), user.get("email"),
    )
    return RedirectResponse(url=f"/templates/{template_id}#tab-tools", status_code=303)


@router.post("/templates/{template_id}/profiles")
async def template_profiles_update(
    template_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Replace the set of tool profiles assigned to this template.
    Admin-only: platform.editor has read-only access to the Agent Registry.

    Accepts a multi-value form field `profile_ids[]`. An empty submission
    clears all profiles (resolve_tool_allowlist falls back to direct tools_allow only).
    """
    pool = get_pool()
    tmpl = await pool.fetchrow("SELECT id FROM templates WHERE id = $1", template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="template not found")

    form = await request.form()
    profile_ids = form.getlist("profile_ids")

    await set_template_profiles(pool, template_id, profile_ids)
    logger.info(
        "Template %s tool profiles updated to [%s] by %s",
        template_id, ", ".join(profile_ids), user.get("email"),
    )
    return RedirectResponse(url=f"/templates/{template_id}", status_code=303)


# -- JSON API ----------------------------------------------------------------

@router.get("/api/v1/templates")
async def api_templates_list(user: dict = Depends(get_current_user)):
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, type, name, description, model, emoji, status, "
        "version, created_at, updated_at FROM templates ORDER BY type, id"
    )
    return JSONResponse([dict(r) for r in rows], status_code=200)


@router.get("/api/v1/templates/{template_id}")
async def api_template_get(template_id: str, user: dict = Depends(get_current_user)):
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM templates WHERE id = $1", template_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="template not found")
    files = await pool.fetch(
        "SELECT filename, content, updated_at FROM template_files "
        "WHERE template_id = $1 ORDER BY filename",
        template_id,
    )
    return JSONResponse(
        {**dict(row), "files": [dict(f) for f in files]},
        status_code=200,
    )
