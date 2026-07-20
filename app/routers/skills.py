"""Skill registry — admin UI + JSON API.

Skills are reusable prompt fragments stored in the `skills` table. Each
skill has a globally-unique `name`, content (markdown body), and an
`applicable_to` array listing which agent template types (GO, SHCA, IA,
RA, Platform, PlatformResearch) it gets injected into.

Versioning: every UPDATE archives the prior state into `skills_history`
before the new content lands. The `version` column on `skills` is the
counter; history rows are immutable.

Edits via this UI write directly to the DB. The .md files under
`agent_files/<owner>/skills/` are the editing surface for git-tracked
development; the `scripts/sync_skills.py` script pushes file changes to
the DB. If a skill exists in both places, the most recent write wins —
running sync_skills.py after a UI edit would re-overwrite from file, so
prefer one workflow per skill.
"""
import hashlib
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin, get_current_user
from app.roles import SystemRole
from app.database import get_pool
from app.services.skill_sync import schedule_propagation


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

# Agent template types a skill can be assigned to.
# Platform and PlatformResearch are mixed-case to match the DB CHECK constraint
# values added in platform_council_agent_types.sql.
ALLOWED_AGENT_TYPES = ("GO", "SHCA", "IA", "RA", "Platform", "PlatformResearch", "PlatformAssistant")

# Case-insensitive lookup → canonical value (preserves mixed-case for Platform types).
_AGENT_TYPE_UPPER_MAP = {t.upper(): t for t in ALLOWED_AGENT_TYPES}

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_applicable(raw: list[str]) -> list[str]:
    """Filter to known types, dedupe, preserve declared order.

    Case-insensitive match; output uses canonical casing from ALLOWED_AGENT_TYPES
    so mixed-case types like Platform/PlatformResearch survive round-trips.
    """
    seen: set[str] = set()
    out: list[str] = []
    for v in raw:
        canonical = _AGENT_TYPE_UPPER_MAP.get((v or "").strip().upper())
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


# -- List view ----------------------------------------------------------------

@router.get("/settings/skills",response_class=HTMLResponse)
async def skills_list(request: Request, user: dict = Depends(require_admin)):
    """Skill registry — table of every skill. Restricted to platform.admin
    (require_admin)."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT name, title, description, version, applicable_to,
               status, content_hash, updated_at, source_path
          FROM skills
         WHERE status <> 'deprecated'
         ORDER BY status DESC, name
        """
    )
    return templates.TemplateResponse(
        "skills/list.html",
        {
            "request": request,
            "user": user,
            "skills": [dict(r) for r in rows],
            "agent_types": ALLOWED_AGENT_TYPES,
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
        },
    )


# -- Create new skill ---------------------------------------------------------
# Admin-only: platform.editor has read-only access to the Skill Registry.

@router.get("/settings/skills/new",response_class=HTMLResponse)
async def skills_new(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(
        "skills/detail.html",
        {
            "request": request,
            "user": user,
            "skill": None,
            "agent_types": ALLOWED_AGENT_TYPES,
            "is_new": True,
            "can_edit": True,
        },
    )


@router.post("/settings/skills")
async def skills_create(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="name must be lowercase letters, digits, underscores; start with a letter; 2-64 chars",
        )

    title = (form.get("title") or "").strip() or name
    description = (form.get("description") or "").strip()
    content = (form.get("content") or "").strip() + "\n"
    applicable_to = _normalize_applicable(form.getlist("applicable_to"))

    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if not content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    if not applicable_to:
        raise HTTPException(
            status_code=400,
            detail="select at least one agent template this skill applies to",
        )

    content_hash = _sha256(content)
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT 1 FROM skills WHERE name = $1", name,
            )
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"skill {name!r} already exists",
                )
            await conn.execute(
                """
                INSERT INTO skills
                    (name, title, description, content, content_hash,
                     version, applicable_to, source_path, status,
                     created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, 1, $6, NULL, 'active',
                        NOW(), NOW())
                """,
                name, title, description, content, content_hash, applicable_to,
            )
            await conn.execute(
                """
                INSERT INTO skills_history
                    (skill_name, version, title, description, content,
                     content_hash, applicable_to, status,
                     changed_by, change_reason)
                VALUES ($1, 1, $2, $3, $4, $5, $6, 'active',
                        $7, 'created via admin UI')
                """,
                name, title, description, content, content_hash, applicable_to,
                user.get("uid", "unknown"),
            )

    # Fan-out: push to every live agent_instances whose template type is
    # in applicable_to. Async — returns immediately, errors logged.
    schedule_propagation(
        skill_name=name,
        content=content,
        old_applicable_to=[],
        new_applicable_to=applicable_to,
        new_status="active",
    )

    return RedirectResponse(url=f"/settings/skills/{name}", status_code=303)


# -- Edit existing skill ------------------------------------------------------

@router.get("/settings/skills/{name}",response_class=HTMLResponse)
async def skills_detail(name: str, request: Request, user: dict = Depends(get_current_user)):
    """Viewable by anyone logged in — the template (skills/detail.html) renders
    the content read-only and hides all edit controls unless can_edit is true
    (admin-only)."""
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM skills WHERE name = $1", name)
    if not row:
        raise HTTPException(status_code=404, detail="skill not found")
    return templates.TemplateResponse(
        "skills/detail.html",
        {
            "request": request,
            "user": user,
            "skill": dict(row),
            "agent_types": ALLOWED_AGENT_TYPES,
            "is_new": False,
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
        },
    )


@router.post("/settings/skills/{name}")
async def skills_update(name: str, request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    title = (form.get("title") or "").strip() or name
    description = (form.get("description") or "").strip()
    content = (form.get("content") or "").strip() + "\n"
    applicable_to = _normalize_applicable(form.getlist("applicable_to"))
    change_reason = (form.get("change_reason") or "").strip() or "edited via admin UI"

    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if not content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    if not applicable_to:
        raise HTTPException(status_code=400, detail="select at least one agent template")

    content_hash = _sha256(content)
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT version, content_hash, title, description, applicable_to, status "
                "FROM skills WHERE name = $1 FOR UPDATE",
                name,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="skill not found")

            old_applicable_to = list(existing["applicable_to"])
            unchanged = (
                existing["content_hash"] == content_hash
                and existing["title"] == title
                and existing["description"] == description
                and old_applicable_to == list(applicable_to)
            )
            if unchanged:
                return RedirectResponse(url=f"/settings/skills/{name}", status_code=303)

            new_version = existing["version"] + 1
            await conn.execute(
                """
                INSERT INTO skills_history
                    (skill_name, version, title, description, content,
                     content_hash, applicable_to, status,
                     changed_by, change_reason)
                SELECT name, version, title, description, content,
                       content_hash, applicable_to, status,
                       $2, $3
                  FROM skills WHERE name = $1
                """,
                name, user.get("uid", "unknown"), change_reason,
            )
            await conn.execute(
                """
                UPDATE skills
                   SET title = $2,
                       description = $3,
                       content = $4,
                       content_hash = $5,
                       version = $6,
                       applicable_to = $7,
                       updated_at = NOW()
                 WHERE name = $1
                """,
                name, title, description, content, content_hash,
                new_version, applicable_to,
            )

    # Fan-out: diff old_applicable_to → applicable_to, push/delete on
    # live instances. Status stays 'active' on edits (deprecation has its
    # own route).
    schedule_propagation(
        skill_name=name,
        content=content,
        old_applicable_to=old_applicable_to,
        new_applicable_to=applicable_to,
        new_status="active",
    )

    return RedirectResponse(url=f"/settings/skills/{name}", status_code=303)


# -- Deprecate / restore ------------------------------------------------------

@router.post("/settings/skills/{name}/deprecate")
async def skills_deprecate(name: str, user: dict = Depends(require_admin)):
    snap = await _set_status(name, "deprecated", user.get("uid", "unknown"), "deprecated via admin UI")
    if snap:
        schedule_propagation(
            skill_name=name,
            content=snap["content"],
            old_applicable_to=list(snap["applicable_to"]),
            new_applicable_to=list(snap["applicable_to"]),
            new_status="deprecated",
        )
    return RedirectResponse(url=f"/settings/skills/{name}", status_code=303)


@router.post("/settings/skills/{name}/restore")
async def skills_restore(name: str, user: dict = Depends(require_admin)):
    snap = await _set_status(name, "active", user.get("uid", "unknown"), "restored via admin UI")
    if snap:
        schedule_propagation(
            skill_name=name,
            content=snap["content"],
            old_applicable_to=[],
            new_applicable_to=list(snap["applicable_to"]),
            new_status="active",
        )
    return RedirectResponse(url=f"/settings/skills/{name}", status_code=303)


async def _set_status(name: str, new_status: str, changed_by: str, reason: str) -> Optional[dict]:
    """Set a skill's status and write a history row. Returns the snapshot
    of the row (content + applicable_to) so the caller can fan out to
    live instances, or None if nothing changed.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM skills WHERE name = $1 FOR UPDATE", name,
            )
            if not row:
                raise HTTPException(status_code=404, detail="skill not found")
            if row["status"] == new_status:
                return None
            new_version = row["version"] + 1
            await conn.execute(
                """
                INSERT INTO skills_history
                    (skill_name, version, title, description, content,
                     content_hash, applicable_to, status,
                     changed_by, change_reason)
                SELECT name, version, title, description, content,
                       content_hash, applicable_to, status,
                       $2, $3
                  FROM skills WHERE name = $1
                """,
                name, changed_by, reason,
            )
            await conn.execute(
                """
                UPDATE skills
                   SET status = $2,
                       version = $3,
                       updated_at = NOW()
                 WHERE name = $1
                """,
                name, new_status, new_version,
            )
            return {"content": row["content"], "applicable_to": row["applicable_to"]}


# -- JSON API (used by other services / agent provisioning) -------------------

@router.get("/api/skills")
async def api_skills_list(
    template: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """List skills, optionally filtered to those applicable to a template.

    Example: GET /api/skills?template=GO → all skills with 'GO' in applicable_to.
    """
    pool = get_pool()
    if template:
        rows = await pool.fetch(
            """
            SELECT name, title, description, version, applicable_to,
                   status, content_hash, updated_at
              FROM skills
             WHERE $1 = ANY(applicable_to) AND status = 'active'
             ORDER BY name
            """,
            template.upper(),
        )
    else:
        rows = await pool.fetch(
            """
            SELECT name, title, description, version, applicable_to,
                   status, content_hash, updated_at
              FROM skills ORDER BY status DESC, name
            """
        )
    return JSONResponse([dict(r) for r in rows], status_code=200)


@router.get("/api/skills/{name}")
async def api_skill_get(name: str, user: dict = Depends(get_current_user)):
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM skills WHERE name = $1", name)
    if not row:
        raise HTTPException(status_code=404, detail="skill not found")
    return JSONResponse(dict(row), status_code=200)
