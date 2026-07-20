"""Agent registry JSON API and global HTML admin view.

JSON API:
  POST   /api/v1/agents              — create agent
  GET    /api/v1/agents              — list active agent instances
  PATCH  /api/v1/agents/{id}         — partial update
  GET    /api/v1/agents/{id}         — get single agent with file list
  GET    /api/v1/agents/{id}/sync-log — last 20 sync log entries
  DELETE /api/v1/agents/{id}         — soft-delete

HTML UI:
  GET /agents  — paginated global agent registry (admin)
"""
import logging
import urllib.parse
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin, require_editor
from app.config import get_settings
from app.database import get_pool
from app.models.agent import AGENT_FILE_TYPES, AgentCreateRequest, AgentUpdateRequest, TOOL_OPTIONS
from app.openclaw import GatewayError, gateway_admin
from app.services.agent_registry import decommission_agent, provision_agent, update_agent

_templates = Jinja2Templates(directory="app/templates")
router = APIRouter()
logger = logging.getLogger(__name__)

# Column subset for list/search queries — excludes large text fields like system prompt.
AGENT_SUMMARY_COLS = """id, name, type, scope, model, emoji, tools_allow, tools_deny,
                 memory_enabled, max_tokens, notes, created_at, created_by,
                 gateway_agent_id, workspace, status,
                 config_version, gateway_synced_version, gateway_synced_at"""

AGENT_PAGE_SIZE = 10


def _agent_list_page_url(after: str = "", stack: str = "", q: str = "") -> str:
    params = {k: v for k, v in [("after", after), ("stack", stack), ("q", q)] if v}
    return "/agents?" + urllib.parse.urlencode(params) if params else "/agents"


# -- JSON API ------------------------------------------------------------------

@router.post("/api/v1/agents")
async def api_create_agent(req: AgentCreateRequest, user: dict = Depends(require_admin)):
    """Create a new agent. Returns the created agent on success.

    Admin-only: platform.editor has read-only access to the Agent Registry."""
    pool = get_pool()
    try:
        agent = await provision_agent(pool, req, user["actor_id"])
        logger.info("Agent created: id=%s type=%s by %s", agent.get("id"), req.type, user.get("actor_id"))
        return JSONResponse({"ok": True, "agent": agent})
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except GatewayError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway error: {exc}")


@router.get("/api/v1/agents")
async def api_list_agents(user: dict = Depends(require_editor)):
    """Return active agent instances as JSON (for tooling and scripts).

    Excludes rows that have a matching entry in the `templates` table —
    those are catalogue entries, served by /api/v1/templates.
    """
    pool = get_pool()
    rows = await pool.fetch(
        f"""SELECT {AGENT_SUMMARY_COLS} FROM agents
            WHERE status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM templates WHERE templates.id = agents.id
              )
            ORDER BY created_at DESC"""
    )
    agents = [dict(r) for r in rows]
    logger.info("Agent instances listed: count=%d by %s", len(agents), user.get("actor_id"))
    return JSONResponse(jsonable_encoder({"agents": agents}))


@router.patch("/api/v1/agents/{agent_id}")
async def api_update_agent(agent_id: str, patch: AgentUpdateRequest, user: dict = Depends(require_admin)):
    """Partially update an agent. Only provided (non-null) fields are changed.

    Admin-only: platform.editor has read-only access to the Agent Registry."""
    pool = get_pool()
    try:
        agent = await update_agent(pool, agent_id, patch, user["actor_id"])
        logger.info("Agent updated via API: id=%s by %s", agent_id, user.get("actor_id"))
        return JSONResponse(jsonable_encoder({"ok": True, "agent": agent}))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GatewayError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway error: {exc}")


@router.get("/api/v1/agents/{agent_id}")
async def api_get_agent(agent_id: str, user: dict = Depends(require_editor)):
    """Return a single agent (all fields) with its workspace file list."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND status = 'active'", agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = dict(row)

    file_rows = await pool.fetch(
        "SELECT filename, written_at FROM agent_files WHERE agent_id = $1 ORDER BY filename",
        agent_id,
    )
    agent["files"] = [dict(f) for f in file_rows]
    logger.debug("Agent fetched via API: id=%s files=%d by %s", agent_id, len(agent["files"]), user.get("actor_id"))
    return JSONResponse(jsonable_encoder(agent))


@router.get("/api/v1/agents/{agent_id}/sync-log")
async def api_agent_sync_log(agent_id: str, user: dict = Depends(require_editor)):
    """Return the last 20 sync operations for an agent."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT from_version, to_version, files_synced, success, error_msg, duration_ms, synced_at
           FROM agent_sync_log WHERE agent_id = $1
           ORDER BY synced_at DESC LIMIT 20""",
        agent_id,
    )
    entries = [dict(r) for r in rows]
    logger.debug("Sync log fetched: agent_id=%s entries=%d by %s", agent_id, len(entries), user.get("actor_id"))
    return JSONResponse(jsonable_encoder({"entries": entries}))


@router.delete("/api/v1/agents/{agent_id}")
async def api_delete_agent(agent_id: str, user: dict = Depends(require_admin)):
    """Soft-delete an agent and destroy its gateway workspace.

    Admin-only: platform.editor has read-only access to the Agent Registry."""
    pool = get_pool()
    try:
        await decommission_agent(pool, agent_id, user["actor_id"])
        logger.info("Agent deleted: id=%s by %s", agent_id, user.get("actor_id"))
        return JSONResponse({"ok": True, "agent_id": agent_id})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# -- HTML UI (redirects to consolidated Agent Registry) -----------------------

@router.get("/agents", response_class=HTMLResponse)
async def list_all_agents(
    request: Request,
    after: str = "",
    stack: str = "",
    q: str = "",
    user: dict = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse as _RR
    params = {k: v for k, v in [("view", "instances"), ("after", after), ("stack", stack), ("q", q)] if v}
    redirect_url = "/settings/agent-registry?" + urllib.parse.urlencode(params)
    logger.debug("list_all_agents: redirecting to %s", redirect_url)
    return _RR(url=redirect_url, status_code=302)
