"""Goal-scoped agent views: list, detail, context, files, and tools.

HTML UI:
  GET    /goals/{goal_id}/agents                         — list agents in goal  [PARKED — returns 404]
  POST   /goals/{goal_id}/agents/{agent_id}              — add agent to goal
  DELETE /goals/{goal_id}/agents/{agent_id}              — remove agent from goal
  GET    /goals/{goal_id}/agents/{agent_id}              — agent detail          [PARKED — returns 404]
  POST   /goals/{goal_id}/agents/{agent_id}/context      — update agent context
  POST   /goals/{goal_id}/agents/{agent_id}/files        — upload workspace file
  POST   /goals/{goal_id}/agents/{agent_id}/tools        — update tool allowlist

PARKED PAGES
------------
The two GET pages (agent list and agent detail) are not linked from the UI and return 404
to prevent accidental navigation. The agent membership data model and mutation endpoints
(POST/DELETE) remain fully functional — they are used internally by provisioning flows.

The UI pages were removed because the goal-scoped agent management workflow is not yet
defined for end users; agent assignment currently happens automatically via provisioning.
Re-enable by removing the 404 guards in list_goal_agents() and agent_detail().
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role
from app.roles import SystemRole, GoalRole
from app.config import get_settings
from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import AGENT_CONTEXT_BOUND
from app.models.agent import AGENT_FILE_TYPES, AgentUpdateRequest, TOOL_OPTIONS
from app.openclaw import gateway_admin
from app.services.agent_registry import update_agent

_templates = Jinja2Templates(directory="app/templates")
router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/goals/{goal_id}/agents", response_class=HTMLResponse)
async def list_goal_agents(
    goal_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Page parked — not linked from UI. Re-enable by removing this line.
    raise HTTPException(status_code=404, detail="Page not available")

    role = await get_goal_role(user["uid"], goal_id)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    rows = await pool.fetch(
        """SELECT gam.agent_id AS id,
                  gac.additional_instructions, gac.skills, gac.workspace_files,
                  a.name, a.type, a.scope, a.model, a.emoji,
                  a.tools_allow, a.tools_deny, a.memory_enabled, a.max_tokens,
                  a.notes, a.gateway_agent_id, a.workspace
           FROM goal_agent_memberships gam
           JOIN agents a ON a.id = gam.agent_id
           LEFT JOIN goal_agent_contexts gac
               ON gac.goal_id = gam.goal_id AND gac.agent_id = gam.agent_id
           WHERE gam.goal_id = $1 AND gam.status = 'active'""",
        goal_id,
    )
    agents = [dict(r) for r in rows]

    goal_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", goal_id)
    goal = dict(goal_row)

    # Get user's assistants not yet in this goal (for the "Add Agent" dropdown)
    from app.agent_types import to_db_agent_type, ASSISTANT as _ASSISTANT_TYPE
    agents_in_goal = {a["id"] for a in agents}
    user_available_assistants = await pool.fetch(
        f"""SELECT a.id AS agent_id, a.name AS display_name, a.type
             FROM agents a
             JOIN users u ON u.actor_id = a.owner_actor_id
            WHERE u.uid = $1 AND a.type = '{to_db_agent_type(_ASSISTANT_TYPE)}' AND a.status = 'active'
            ORDER BY a.name""",
        user["uid"],
    )
    available_assistants = [
        dict(r) for r in user_available_assistants
        if r["agent_id"] not in agents_in_goal
    ]

    # All active IA/RA agents not yet in this goal
    all_workers = await pool.fetch(
        """SELECT id, name, type, emoji, model
           FROM agents
           WHERE status = 'active' AND type IN ('IA', 'RA')
           ORDER BY type, name""",
    )
    available_workers = [dict(r) for r in all_workers if r["id"] not in agents_in_goal]

    logger.debug(
        "Goal agents listed: goal_id=%s agents=%d available_assistants=%d available_workers=%d by %s",
        goal_id, len(agents), len(available_assistants), len(available_workers), user.get("uid"),
    )

    return _templates.TemplateResponse(
        "agents/goal_list.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": goal_id,
            "agents": agents,
            "role": role,
            "available_assistants": available_assistants,
            "available_workers": available_workers,
        },
    )


@router.post("/goals/{goal_id}/agents/{agent_id}")
async def add_agent_to_goal(
    goal_id: str,
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], goal_id)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    exists = await pool.fetchval("SELECT id FROM agents WHERE id = $1 AND status = 'active'", agent_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Agent not found")

    form = await request.form()
    additional_instructions = form.get("additional_instructions", "")
    skills = form.getlist("skills")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=AGENT_CONTEXT_BOUND,
                payload={
                    "goal_id": goal_id,
                    "agent_id": agent_id,
                    "additional_instructions": additional_instructions,
                    "skills": skills,
                    "added_by": user["uid"],
                },
                actor_id=user["actor_id"],
                context_goal_id=goal_id,
            )
            await conn.execute(
                """INSERT INTO goal_agent_memberships (goal_id, agent_id, status, added_at, added_by)
                   VALUES ($1, $2, 'active', now(), $3)
                   ON CONFLICT (goal_id, agent_id) DO NOTHING""",
                goal_id, agent_id, user["actor_id"],
            )
            await conn.execute(
                """INSERT INTO goal_agent_contexts
                       (goal_id, agent_id, additional_instructions, skills, updated_by)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (goal_id, agent_id) DO NOTHING""",
                goal_id, agent_id, additional_instructions, skills, user["actor_id"],
            )

    logger.info(
        "Agent added to goal: goal_id=%s agent_id=%s skills=%s by %s",
        goal_id, agent_id, skills, user.get("actor_id"),
    )
    return RedirectResponse(url=f"/goals/{goal_id}/agents", status_code=303)


@router.delete("/goals/{goal_id}/agents/{agent_id}")
async def remove_agent_from_goal(
    goal_id: str,
    agent_id: str,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], goal_id)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    await pool.execute(
        """UPDATE goal_agent_memberships
           SET status = 'removed', removed_at = now(), removed_by = $1
           WHERE goal_id = $2 AND agent_id = $3""",
        user["actor_id"], goal_id, agent_id,
    )
    logger.info(
        "Agent removed from goal: goal_id=%s agent_id=%s by %s",
        goal_id, agent_id, user.get("actor_id"),
    )
    return JSONResponse({"ok": True})


@router.get("/goals/{goal_id}/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail(
    goal_id: str,
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Page parked — not linked from UI. Re-enable by removing this line.
    raise HTTPException(status_code=404, detail="Page not available")

    role = await get_goal_role(user["uid"], goal_id)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    agent_row = await pool.fetchrow(
        """SELECT id, name, type, scope, model, emoji, tools_allow, tools_deny,
                  memory_enabled, max_tokens, notes, gateway_agent_id, workspace,
                  status, config_version, gateway_synced_version
           FROM agents WHERE id = $1""",
        agent_id,
    )
    if not agent_row:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = dict(agent_row)

    file_rows = await pool.fetch(
        "SELECT filename, content FROM agent_files WHERE agent_id = $1 AND filename = ANY($2)",
        agent_id, ["AGENTS.md", "SOUL.md"],
    )
    files_by_name = {r["filename"]: r["content"] for r in file_rows}
    agent["base_system_prompt"] = files_by_name.get("AGENTS.md", "")
    agent["soul"] = files_by_name.get("SOUL.md", "")

    ctx_row = await pool.fetchrow(
        "SELECT * FROM goal_agent_contexts WHERE goal_id = $1 AND agent_id = $2",
        goal_id, agent_id,
    )
    context = dict(ctx_row) if ctx_row else {}

    db_file_rows = await pool.fetch(
        "SELECT filename, gateway_synced_at FROM agent_files WHERE agent_id = $1",
        agent_id,
    )
    file_sync_times = {r["filename"]: r["gateway_synced_at"] for r in db_file_rows}

    goal_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", goal_id)
    goal = dict(goal_row)

    thread_rows = await pool.fetch(
        """SELECT * FROM threads
           WHERE goal_id = $1 AND agent_id = $2
           ORDER BY created_at DESC LIMIT 20""",
        goal_id, agent_id,
    )
    threads = [dict(r) for r in thread_rows]

    gateway_agent_id = agent.get("gateway_agent_id") or agent_id
    agent_files = []
    try:
        files_result = await gateway_admin.list_agent_files(gateway_agent_id)
        agent_files = files_result.get("files", [])
    except Exception as exc:
        logger.warning("Failed to fetch agent files for %s: %s", gateway_agent_id, exc)

    logger.debug(
        "Agent detail rendered: goal_id=%s agent_id=%s threads=%d by %s",
        goal_id, agent_id, len(threads), user.get("uid"),
    )

    return _templates.TemplateResponse(
        "agents/detail.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": goal_id,
            "agent": agent,
            "context": context,
            "threads": threads,
            "role": role,
            "models": get_settings().available_models,
            "tool_options": TOOL_OPTIONS,
            "file_types": AGENT_FILE_TYPES,
            "agent_files": agent_files,
            "file_sync_times": file_sync_times,
        },
    )


@router.post("/goals/{goal_id}/agents/{agent_id}/context")
async def update_agent_context(
    goal_id: str,
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], goal_id)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    form = await request.form()
    pool = get_pool()
    await pool.execute(
        """UPDATE goal_agent_contexts
           SET additional_instructions = $1, skills = $2,
               config_version = config_version + 1, updated_at = now(), updated_by = $3
           WHERE goal_id = $4 AND agent_id = $5""",
        form.get("additional_instructions", ""), form.getlist("skills"),
        user["actor_id"], goal_id, agent_id,
    )
    logger.info(
        "Agent context updated: goal_id=%s agent_id=%s by %s",
        goal_id, agent_id, user.get("actor_id"),
    )
    return RedirectResponse(url=f"/goals/{goal_id}/agents/{agent_id}", status_code=303)


@router.post("/goals/{goal_id}/agents/{agent_id}/files")
async def upload_workspace_file(
    goal_id: str,
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], goal_id)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    from app.storage import upload_workspace_file as gcs_upload
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")

    contents = await file.read()
    uri = await gcs_upload(goal_id, agent_id, file.filename, contents)

    pool = get_pool()
    await pool.execute(
        """UPDATE goal_agent_contexts
           SET workspace_files = array_append(workspace_files, $1),
               config_version = config_version + 1, updated_at = now(), updated_by = $2
           WHERE goal_id = $3 AND agent_id = $4""",
        uri, user["actor_id"], goal_id, agent_id,
    )
    logger.info(
        "Workspace file uploaded: goal_id=%s agent_id=%s filename=%s uri=%s by %s",
        goal_id, agent_id, file.filename, uri, user.get("actor_id"),
    )
    return RedirectResponse(url=f"/goals/{goal_id}/agents/{agent_id}", status_code=303)


@router.post("/goals/{goal_id}/agents/{agent_id}/tools")
async def update_agent_tools(
    goal_id: str,
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Update the tool allowlist for an agent.

    Empty list = all tools allowed (default).
    Non-empty list = only those tools allowed, everything else denied.
    Routes through the service to maintain config_version and audit trail.
    """
    role = await get_goal_role(user["uid"], goal_id)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    form = await request.form()
    tools_allow = form.getlist("tools_allow")

    pool = get_pool()
    patch = AgentUpdateRequest(tools_allow=tools_allow)
    try:
        await update_agent(pool, agent_id, patch, user["actor_id"])
        logger.info(
            "Agent tools updated: goal_id=%s agent_id=%s tools_count=%d by %s",
            goal_id, agent_id, len(tools_allow), user.get("actor_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return RedirectResponse(url=f"/goals/{goal_id}/agents/{agent_id}", status_code=303)
