"""Consolidated Agent Registry page.

Replaces the three separate list pages:
  /templates            → GET /agent-registry?view=templates
  /agents               → GET /agent-registry?view=instances
  /admin/shca-templates → GET /agent-registry?view=templates&type=SHCA

JSON API routes in registry_routes.py and templates.py are unchanged.
"""
import logging
import urllib.parse
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin, get_current_user
from app.roles import SystemRole
from app.config import get_settings
from app.database import get_pool
from app.models.agent import TOOL_OPTIONS
from app.openclaw import gateway_admin

router = APIRouter()
_templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

VALID_VIEWS = ("templates", "instances")
VALID_TYPES = ("", "GO", "SHCA", "IA", "RA", "Platform", "PlatformResearch")
VALID_PAGE_SIZES = (5, 10, 15)
DEFAULT_PAGE_SIZE = 10


def _instances_url(after: str = "", stack: str = "", q: str = "", type_: str = "", per: int = DEFAULT_PAGE_SIZE) -> str:
    params = {k: v for k, v in [
        ("view", "instances"),
        ("after", after),
        ("stack", stack),
        ("q", q),
        ("type", type_),
        ("per", str(per) if per != DEFAULT_PAGE_SIZE else ""),
    ] if v}
    return "/settings/agent-registry?" + urllib.parse.urlencode(params)


def _templates_url(tpage: int = 1, q: str = "", type_: str = "", per: int = DEFAULT_PAGE_SIZE) -> str:
    params = {k: v for k, v in [
        ("view", "templates"),
        ("tpage", str(tpage) if tpage > 1 else ""),
        ("q", q),
        ("type", type_),
        ("per", str(per) if per != DEFAULT_PAGE_SIZE else ""),
    ] if v}
    return "/settings/agent-registry?" + urllib.parse.urlencode(params)


@router.get("/settings/agent-registry", response_class=HTMLResponse)
async def agent_registry(
    request: Request,
    view: str = "templates",
    type: str = "",
    q: str = "",
    after: str = "",
    stack: str = "",
    per: int = DEFAULT_PAGE_SIZE,
    tpage: int = 1,
    user: dict = Depends(require_admin),
):
    """Unified agent registry — templates and instances in one page.
    Restricted to platform.admin (require_admin); can_edit further gates the
    mutating controls rendered by agent_registry/index.html."""
    pool = get_pool()

    if view not in VALID_VIEWS:
        view = "templates"
    if type not in VALID_TYPES:
        type = ""
    if per not in VALID_PAGE_SIZES:
        per = DEFAULT_PAGE_SIZE
    if tpage < 1:
        tpage = 1

    # ── Gateway header strip ──────────────────────────────────────────────
    try:
        gw_row = await pool.fetchrow(
            """SELECT id, name, status, current_agents, last_heartbeat_at
               FROM gateways
               ORDER BY last_heartbeat_at DESC NULLS LAST
               LIMIT 1"""
        )
        gateway = dict(gw_row) if gw_row else None
    except Exception:
        gateway = None
        logger.debug("agent_registry: gateways table not available")

    # ── Templates data (paginated) ────────────────────────────────────────
    type_filter = type if type else None
    tmpl_total = await pool.fetchval(
        """SELECT COUNT(*) FROM templates t
           WHERE ($1::text IS NULL OR t.type = $1)
             AND ($2::text = '' OR t.id ILIKE $3 OR t.name ILIKE $3)
             -- Base templates only. Hide per-user assistant templates (legacy
             -- shca-user-* rows) — the ONLY templates with a parent_template_id.
             -- They are per-user instance config, not reusable blueprints.
             -- NB: owner_actor_id is NOT the discriminator — some system
             -- blueprints (e.g. platform-shca) carry a sentinel owner but no
             -- parent and must stay visible.
             AND t.parent_template_id IS NULL""",
        type_filter,
        q,
        f"%{q}%" if q else "",
    )
    rows = await pool.fetch(
        f"""
        SELECT t.id, t.type, t.name, t.description, t.model, t.emoji,
               t.status, t.version, t.updated_at,
               (SELECT COUNT(*) FROM agents a
                 WHERE a.template_id = t.id AND a.status = 'active'
                   AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = a.id)
               ) + (SELECT COUNT(*) FROM agent_instances ai
                 WHERE ai.template_id = t.id AND ai.status = 'active'
               ) AS instance_count,
               (SELECT COUNT(*) FROM agents a
                 WHERE a.template_id = t.id AND a.status = 'active'
                   AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = a.id)
                   AND a.gateway_synced_version < a.config_version
               ) AS drift_count,
               (SELECT COUNT(*) FROM template_files tf
                 WHERE tf.template_id = t.id) AS file_count,
               (SELECT COUNT(*) FROM skills s
                 WHERE t.type = ANY(s.applicable_to) AND s.status = 'active'
               ) AS skill_count
          FROM templates t
         WHERE ($1::text IS NULL OR t.type = $1)
           AND ($2::text = '' OR t.id ILIKE $3 OR t.name ILIKE $3)
           AND t.parent_template_id IS NULL   -- base templates only (see COUNT above)
         ORDER BY t.status DESC, t.type, t.id
         LIMIT {per} OFFSET {(tpage - 1) * per}
        """,
        type_filter,
        q,
        f"%{q}%" if q else "",
    )
    templates_data = [dict(r) for r in rows]

    tmpl_has_next = (tpage * per) < tmpl_total
    tmpl_page_start = (tpage - 1) * per + 1 if templates_data else 0
    tmpl_page_end = tmpl_page_start + len(templates_data) - 1 if templates_data else 0
    tmpl_prev_url = _templates_url(tpage - 1, q, type, per) if tpage > 1 else ""
    tmpl_next_url = _templates_url(tpage + 1, q, type, per) if tmpl_has_next else ""

    logger.debug(
        "agent_registry templates: page=%d per=%d count=%d total=%d view=%s type=%s q=%s by %s",
        tpage, per, len(templates_data), tmpl_total, view, type or "all", repr(q) if q else "none", user.get("actor_id"),
    )

    # ── Instances data (paginated) ────────────────────────────────────────
    agents = []
    total = 0
    prev_url = next_url = ""
    page_num = 1
    page_start = page_end = 0

    if q and view == "instances":
        after = stack = ""

    cursor_ts: datetime | None = None
    cursor_id: str | None = None
    if after:
        try:
            ts_str, cursor_id = after.split("__", 1)
            cursor_ts = datetime.fromisoformat(ts_str)
        except (ValueError, AttributeError):
            after = ""

    # CTE unions persistent agents (GO/SHCA from agents table) with task-scoped
    # instances (IA/RA from agent_instances). Both sides exclude deleted goals.
    instances_cte = """
        WITH instances AS (
            SELECT a.id, a.name, a.type, a.scope, a.model, a.emoji,
                   a.tools_allow, a.memory_enabled, a.created_at, a.status,
                   a.config_version, a.gateway_synced_version, a.gateway_synced_at,
                   b.last_sync_error, b.last_synced_at AS binding_last_synced_at
            FROM agents a
            LEFT JOIN agent_gateway_bindings b ON b.agent_id = a.id
            WHERE a.status = 'active'
              AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = a.id)
              AND EXISTS (
                  SELECT 1 FROM agent_contexts ac
                  JOIN goals g ON g.id = ac.goal_id
                  WHERE ac.agent_id = a.id AND g.status != 'deleted'
              )
            UNION ALL
            SELECT ai.id::text, t.name, t.type, t.scope,
                   COALESCE(NULLIF(ai.model, ''), t.model),
                   t.emoji, t.tools_allow, t.memory_enabled, ai.created_at, ai.status,
                   NULL::int, NULL::int, NULL::timestamptz,
                   NULL::text, NULL::timestamptz
            FROM agent_instances ai
            JOIN agents t ON t.id = ai.template_id
            JOIN goals g ON g.id = ai.goal_id
            WHERE ai.status = 'active'
              AND g.status != 'deleted'
        )
    """

    if q and view == "instances":
        rows = await pool.fetch(
            f"""{instances_cte}
                SELECT * FROM instances
                WHERE ($1::text = '' OR type = $1)
                  AND (id ILIKE $2 OR name ILIKE $2 OR type ILIKE $2 OR model ILIKE $2)
                ORDER BY created_at DESC, id ASC
                LIMIT {per + 1}""",
            type or "",
            f"%{q}%",
        )
    elif cursor_ts and cursor_id and view == "instances":
        rows = await pool.fetch(
            f"""{instances_cte}
                SELECT * FROM instances
                WHERE ($1::text = '' OR type = $1)
                  AND (created_at < $2 OR (created_at = $2 AND id > $3))
                ORDER BY created_at DESC, id ASC
                LIMIT {per + 1}""",
            type or "",
            cursor_ts,
            cursor_id,
        )
    else:
        rows = await pool.fetch(
            f"""{instances_cte}
                SELECT * FROM instances
                WHERE ($1::text = '' OR type = $1)
                ORDER BY created_at DESC, id ASC
                LIMIT {per + 1}""",
            type or "",
        )

    agents = [dict(r) for r in rows]
    has_next = len(agents) > per
    if has_next:
        agents = agents[:per]

    next_cursor = ""
    if has_next and agents:
        last = agents[-1]
        next_cursor = f"{last['created_at'].isoformat()}__{last['id']}"

    stack_list = [s for s in stack.split("|") if s]
    if has_next:
        next_url = _instances_url(next_cursor, "|".join([after or "~"] + stack_list), q, type, per)
    if stack_list:
        prev_raw = stack_list[0]
        prev_url = _instances_url("" if prev_raw == "~" else prev_raw, "|".join(stack_list[1:]), q, type, per)

    if q and view == "instances":
        total = await pool.fetchval(
            f"""{instances_cte}
                SELECT COUNT(*) FROM instances
                WHERE ($1::text = '' OR type = $1)
                  AND (id ILIKE $2 OR name ILIKE $2 OR type ILIKE $2 OR model ILIKE $2)""",
            type or "",
            f"%{q}%",
        )
    else:
        total = await pool.fetchval(
            f"""{instances_cte}
                SELECT COUNT(*) FROM instances
                WHERE ($1::text = '' OR type = $1)""",
            type or "",
        )

    page_num = len(stack_list) + 1
    page_start = (page_num - 1) * per + 1
    page_end = page_start + len(agents) - 1

    logger.debug(
        "agent_registry instances: page=%d per=%d total=%d type=%s q=%s by %s",
        page_num, per, total, type or "all", repr(q) if q else "none", user.get("actor_id"),
    )

    # ── Gateway models (used on instance detail page too) ─────────────────
    try:
        gw_models = await gateway_admin.list_models()
        models = [m["id"] for m in gw_models.get("models", [])]
    except Exception:
        models = get_settings().available_models

    return _templates.TemplateResponse(
        "agent_registry/index.html",
        {
            "request": request,
            "user": user,
            "view": view,
            "type_filter": type,
            "q": q,
            "per": per,
            "gateway": gateway,
            # templates pagination
            "templates_data": templates_data,
            "tmpl_total": tmpl_total,
            "tpage": tpage,
            "tmpl_page_start": tmpl_page_start,
            "tmpl_page_end": tmpl_page_end,
            "tmpl_has_next": tmpl_has_next,
            "tmpl_next_url": tmpl_next_url,
            "tmpl_prev_url": tmpl_prev_url,
            # instances pagination
            "agents": agents,
            "total": total,
            "page_num": page_num,
            "page_start": page_start,
            "page_end": page_end,
            "has_next": has_next,
            "next_url": next_url,
            "prev_url": prev_url,
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
        },
    )


@router.get("/settings/agent-registry/instances/{agent_id}", response_class=HTMLResponse)
async def instance_detail(
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Detail / edit page for a single agent instance. Viewable by anyone
    logged in; can_edit (admin-only) gates the edit controls."""
    pool = get_pool()

    row = await pool.fetchrow(
        """SELECT a.*, b.last_sync_error, b.last_synced_at AS binding_last_synced_at
             FROM agents a
             LEFT JOIN agent_gateway_bindings b ON b.agent_id = a.id
            WHERE a.id = $1 AND a.status = 'active'""",
        agent_id,
    )
    is_runtime_instance = False
    if not row:
        # IA / RA instances live in agent_instances — display fields come from the template row.
        # base_system_prompt / soul are not selected here; they are populated below from
        # template_files to avoid reading the deprecated agents columns.
        row = await pool.fetchrow(
            """SELECT ai.id::text AS id, t.id AS template_id, t.name, t.type, t.scope,
                      COALESCE(NULLIF(ai.model, ''), t.model) AS model,
                      t.emoji, t.tools_allow, t.tools_deny, t.memory_enabled,
                      t.max_tokens, t.notes,
                      ai.created_at, ai.status, ai.task_id, ai.goal_id,
                      NULL::int AS config_version, NULL::int AS gateway_synced_version,
                      NULL::timestamptz AS gateway_synced_at,
                      NULL::text AS last_sync_error, NULL::timestamptz AS binding_last_synced_at
                 FROM agent_instances ai
                 JOIN agents t ON t.id = ai.template_id
                WHERE ai.id::text = $1 AND ai.status = 'active'""",
            agent_id,
        )
        is_runtime_instance = True
    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    agent = dict(row)

    # Populate base_system_prompt / soul from the canonical file store rather than
    # the deprecated agents columns (which are no longer written for new instances).
    if is_runtime_instance:
        # IA/RA: files live on the template row in template_files
        template_id = agent.get("template_id")
        file_rows = await pool.fetch(
            "SELECT filename, content FROM template_files WHERE template_id = $1 AND filename = ANY($2)",
            template_id, ["AGENTS.md", "SOUL.md"],
        ) if template_id else []
    else:
        # SHCA/GO: files live on the instance row in agent_files
        file_rows = await pool.fetch(
            "SELECT filename, content FROM agent_files WHERE agent_id = $1 AND filename = ANY($2)",
            agent_id, ["AGENTS.md", "SOUL.md"],
        )
    files_by_name = {r["filename"]: r["content"] for r in file_rows}
    # Prefer agent_files (canonical); fall back to deprecated columns for SHCA/GO
    # agents that pre-date the file store migration (agents with no agent_files rows).
    agent["base_system_prompt"] = files_by_name.get("AGENTS.md") or agent.get("base_system_prompt") or ""
    agent["soul"] = files_by_name.get("SOUL.md") or agent.get("soul") or ""

    try:
        gw_models = await gateway_admin.list_models()
        models = [m["id"] for m in gw_models.get("models", [])]
    except Exception:
        models = get_settings().available_models

    logger.debug("instance_detail: agent_id=%s by %s", agent_id, user.get("actor_id"))

    return _templates.TemplateResponse(
        "agent_registry/instance_detail.html",
        {
            "request": request,
            "user": user,
            "agent": agent,
            "models": models,
            "tool_options": TOOL_OPTIONS,
            "is_runtime_instance": is_runtime_instance,
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
        },
    )
