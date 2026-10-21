"""Guided "New Template" wizard — supports IA and RA template types.

Flow: initial questions → Gemini populates → human edits → create.

  GET  /settings/agent-registry/new           — render the wizard form (step 1)
  POST /settings/agent-registry/new/generate  — Gemini-populate + assemble,
                                                 return the editable review partial (step 2, HTMX swap)
  POST /settings/agent-registry/new/create    — validate + persist a new template (step 3)

Agent type selection (IA / RA) drives archetype selection automatically:
  IA → coding-pipeline archetype
  RA → code-reviewer archetype

Registered BEFORE proxy_routes in main.py so these /settings/* paths are
served by the main service.
"""
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.database import get_pool
from app.routers.templates import _record_history
from app.services.model_registry.dropdowns import (
    is_dropdown_empty,
    list_model_ids_for_dropdown_v2,
)
from app.services.template_authoring import (
    ARCHETYPES,
    WizardInputs,
    assemble,
    generate_role_sections,
)
from app.services.template_authoring.models import GeneratedSections
from app.services.tool_registry import get_profile, set_template_profiles

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

# Default tool profile for IA templates (seeded by migration 045). Applied with
# a missing-profile guard so a create never fails if the seed is absent.
DEFAULT_IA_PROFILE = "task-worker"

# Stub content for the per-instance / per-user files an IA template carries
# alongside the generated AGENTS.md + SOUL.md.
_USER_MD_STUB = (
    "# User\n\nTask-scoped agent — no persistent user profile. Context for each "
    "task arrives in the dispatch message and RESOURCES.md.\n"
)
_TOOLS_MD_STUB = (
    "# Tools\n\nDocument any custom tools or APIs this agent uses here. "
    "Standard platform tools are described in the agent's operating instructions.\n"
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _inputs_from_form(form) -> WizardInputs:
    """Build WizardInputs from a posted form, normalising blanks.

    Archetype is derived from agent_type automatically — the form no longer
    exposes the archetype selector.  IA → coding-pipeline; RA → code-reviewer.
    """
    agent_type = (form.get("agent_type") or "IA").strip().upper()
    if agent_type not in ("IA", "RA"):
        agent_type = "IA"
    archetype = "code-reviewer" if agent_type == "RA" else "coding-pipeline"
    return WizardInputs(
        template_id=(form.get("template_id") or "").strip().lower(),
        name=(form.get("name") or "").strip(),
        emoji=(form.get("emoji") or "").strip(),
        model=(form.get("model") or "google/gemini-3.8-flash").strip(),
        description=(form.get("description") or "").strip(),
        agent_type=agent_type,
        archetype=archetype,
        role_oneliner=(form.get("role_oneliner") or "").strip(),
        role_slug=(form.get("role_slug") or "").strip(),
        previous_agent=(form.get("previous_agent") or "").strip(),
        next_agent=(form.get("next_agent") or "").strip(),
        output_artifact=(form.get("output_artifact") or "").strip(),
        domain=(form.get("domain") or "").strip(),
        personality_hint=(form.get("personality_hint") or "").strip(),
        working_specifics=(form.get("working_specifics") or "").strip(),
    )


# ── Step 1: wizard form ──────────────────────────────────────────────────────

@router.get("/settings/agent-registry/new", response_class=HTMLResponse)
async def template_new(request: Request, user: dict = Depends(require_admin)):
    """Admin-only: platform.editor has read-only access to the Agent Registry."""
    logger.debug("template_authoring: wizard opened by %s", user.get("email"))
    models = await list_model_ids_for_dropdown_v2(get_pool())
    return templates.TemplateResponse(
        "authoring/wizard.html",
        {
            "request": request,
            "user": user,
            "archetypes": ARCHETYPES,
            "models": models,
            "models_empty": is_dropdown_empty(models),
        },
    )


# ── Step 2: generate + assemble → review partial ────────────────────────────

@router.post("/settings/agent-registry/new/generate", response_class=HTMLResponse)
async def template_new_generate(request: Request, user: dict = Depends(require_admin)):
    """Admin-only: platform.editor has read-only access to the Agent Registry."""
    form = await request.form()
    inputs = _inputs_from_form(form)

    if not inputs.template_id or not inputs.name:
        raise HTTPException(status_code=400, detail="template id and name are required")
    if inputs.archetype not in ARCHETYPES:
        raise HTTPException(status_code=400, detail=f"unknown archetype {inputs.archetype!r}")

    sections = await generate_role_sections(inputs.archetype, inputs)
    files = assemble(inputs.archetype, inputs, sections)

    logger.info(
        "template_authoring: generated draft for id=%s archetype=%s ai=%s by %s",
        inputs.template_id, inputs.archetype, sections.ai_generated, user.get("email"),
    )
    return templates.TemplateResponse(
        "authoring/_review.html",
        {
            "request": request,
            "user": user,
            "inputs": inputs,
            "files": files,
            "ai_generated": sections.ai_generated,
        },
    )


# ── Step 3: create the template ──────────────────────────────────────────────

@router.post("/settings/agent-registry/new/create")
async def template_new_create(request: Request, user: dict = Depends(require_admin)):
    """Admin-only: platform.editor has read-only access to the Agent Registry."""
    form = await request.form()
    template_id = (form.get("template_id") or "").strip().lower()
    name = (form.get("name") or "").strip()
    model = (form.get("model") or "").strip()
    emoji = (form.get("emoji") or "").strip()
    description = (form.get("description") or "").strip()
    agent_type = (form.get("agent_type") or "IA").strip().upper()
    agents_md = form.get("agents_md") or ""
    soul_md = form.get("soul_md") or ""

    # ── Validate ──
    if not _SLUG_RE.match(template_id):
        raise HTTPException(
            status_code=400,
            detail="template id must be lowercase letters, digits, and hyphens",
        )
    if agent_type not in ("IA", "RA"):
        raise HTTPException(status_code=400, detail=f"unknown agent type {agent_type!r}")
    if not name or not model:
        raise HTTPException(status_code=400, detail="name and model are required")
    if not agents_md.strip() or not soul_md.strip():
        raise HTTPException(status_code=400, detail="AGENTS.md and SOUL.md must not be empty")

    pool = get_pool()
    files = {
        "AGENTS.md": agents_md,
        "SOUL.md": soul_md,
        "USER.md": _USER_MD_STUB,
        "TOOLS.md": _TOOLS_MD_STUB,
    }

    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM templates WHERE id = $1", template_id)
            if exists:
                raise HTTPException(status_code=409, detail=f"template '{template_id}' already exists")

            await conn.execute(
                """
                INSERT INTO templates
                    (id, type, name, description, model, emoji, status, version)
                VALUES ($1, $2, $3, $4, $5, $6, 'active', 1)
                """,
                template_id, agent_type, name, description, model, emoji,
            )
            for filename, content in files.items():
                await conn.execute(
                    """
                    INSERT INTO template_files (template_id, filename, content, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    """,
                    template_id, filename, content,
                )
            await _record_history(
                conn, template_id, 1,
                {
                    "type": agent_type,
                    "name": name,
                    "model": model,
                    "emoji": emoji,
                    "files": files,
                },
                user.get("uid", "unknown"),
                "created via New Template wizard",
            )

    # ── Default tool profile (post-commit; guard if the seed is missing) ──
    try:
        if await get_profile(pool, DEFAULT_IA_PROFILE):
            await set_template_profiles(pool, template_id, [DEFAULT_IA_PROFILE])
        else:
            logger.warning(
                "template_authoring: default profile %r not found — created %s with no profile",
                DEFAULT_IA_PROFILE, template_id,
            )
    except Exception as exc:
        logger.warning(
            "template_authoring: failed to assign default profile to %s: %s",
            template_id, exc,
        )

    logger.info("template_authoring: created %s template %s by %s", agent_type, template_id, user.get("email"))
    return RedirectResponse(url=f"/templates/{template_id}", status_code=303)
