"""Artifact Registry -- CRUD for goal resources + per-agent access control + PEP."""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role, require_internal_key
from app.roles import SystemRole, GoalRole
from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import (
    RESOURCE_ADDED, RESOURCE_UPDATED, RESOURCE_REMOVED,
    REGISTRY_ACCESS_GRANTED, REGISTRY_ACCESS_UPDATED, REGISTRY_ACCESS_REVOKED,
)
from app.github_tokens import list_installation_repos, list_repo_branches
from app.openclaw import gateway_admin
from app.resource_provisioning import sync_resources_md

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

ACCESS_LEVELS = {"none": 0, "read": 1, "read-write": 2}


# ── GCP Discovery ──────────────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/discover")
async def discover_resources(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Discover GCP resources (BQ datasets, GCS buckets) from a project.

    Body:
      { "type": "bigquery", "gcp_project": "project-id" }
      { "type": "bigquery_tables", "gcp_project": "project-id", "dataset": "analytics" }
      { "type": "gcs", "gcp_project": "project-id" }
      { "type": "gcs_contents", "gcp_project": "project-id", "bucket": "my-bucket", "prefix": "data/" }
    """
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    body = await request.json()
    discovery_type = body.get("type", "")
    gcp_project = body.get("gcp_project", "")

    from app.gcp_discovery import (
        discover_gcp_projects,
        discover_bigquery_datasets, discover_bigquery_tables,
        discover_gcs_buckets, discover_gcs_contents,
    )

    # Projects discovery doesn't need a gcp_project
    if discovery_type == "projects":
        projects = await discover_gcp_projects()
        return JSONResponse({"projects": projects})

    if not gcp_project:
        raise HTTPException(status_code=400, detail="gcp_project is required")

    if discovery_type == "bigquery":
        datasets = await discover_bigquery_datasets(gcp_project)
        return JSONResponse({"gcp_project": gcp_project, "datasets": datasets})

    elif discovery_type == "bigquery_tables":
        dataset = body.get("dataset", "")
        if not dataset:
            raise HTTPException(status_code=400, detail="dataset is required")
        tables = await discover_bigquery_tables(gcp_project, dataset)
        return JSONResponse({"gcp_project": gcp_project, "dataset": dataset, "tables": tables})

    elif discovery_type == "gcs":
        buckets = await discover_gcs_buckets(gcp_project)
        return JSONResponse({"gcp_project": gcp_project, "buckets": buckets})

    elif discovery_type == "gcs_contents":
        bucket = body.get("bucket", "")
        if not bucket:
            raise HTTPException(status_code=400, detail="bucket is required")
        prefix = body.get("prefix", "")
        contents = await discover_gcs_contents(gcp_project, bucket, prefix)
        return JSONResponse({"gcp_project": gcp_project, "bucket": bucket, "prefix": prefix, **contents})

    else:
        raise HTTPException(status_code=400, detail=f"Unknown discovery type: {discovery_type}")


@router.post("/goals/{pid}/resources/add-cloud")
async def add_cloud_resource(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Add a GCS bucket or BigQuery dataset as a goal resource.

    Body:
      { "type": "gcs_bucket", "name": "Design Assets", "gcp_project": "project-456",
        "bucket": "design-assets", "path_prefix": "designs/" }
      { "type": "bigquery_dataset", "name": "Analytics", "gcp_project": "project-123",
        "dataset": "analytics" }
    """
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    body = await request.json()
    rtype = body.get("type", "")
    name = body.get("name", "").strip()
    gcp_project = body.get("gcp_project", "").strip()

    if not name or not gcp_project or not rtype:
        raise HTTPException(status_code=400, detail="name, type, gcp_project are required")

    pool = get_pool()
    resource_id = str(uuid.uuid4())[:12]

    if rtype == "gcs_bucket":
        bucket = body.get("bucket", "").strip()
        path_prefix = body.get("path_prefix", "").strip()
        if not bucket:
            raise HTTPException(status_code=400, detail="bucket is required")
        uri = f"gs://{bucket}/{path_prefix}"

        await pool.execute(
            """INSERT INTO resources (id, goal_id, name, type, uri, gcp_project, bucket, path_prefix,
                                     default_access, added_by, notes)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'read', $9, $10)""",
            resource_id, pid, name, rtype, uri, gcp_project, bucket, path_prefix,
            user["uid"], body.get("notes", ""),
        )

    elif rtype == "bigquery_dataset":
        dataset = body.get("dataset", "").strip()
        if not dataset:
            raise HTTPException(status_code=400, detail="dataset is required")
        uri = f"bigquery://{gcp_project}/{dataset}"

        await pool.execute(
            """INSERT INTO resources (id, goal_id, name, type, uri, gcp_project, dataset,
                                     default_access, added_by, notes)
               VALUES ($1, $2, $3, $4, $5, $6, $7, 'read', $8, $9)""",
            resource_id, pid, name, rtype, uri, gcp_project, dataset,
            user["uid"], body.get("notes", ""),
        )

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported cloud type: {rtype}")

    # Ledger event
    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user["uid"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=RESOURCE_ADDED,
                payload={
                    "resource_id": resource_id,
                    "name": name,
                    "type": rtype,
                    "uri": uri,
                    "gcp_project": gcp_project,
                },
                actor_id=user_actor_id,
                context_goal_id=pid,
            )

    _log.info("Added cloud resource '%s' (%s) to goal %s", name, rtype, pid)
    return JSONResponse({"ok": True, "resource_id": resource_id, "name": name, "type": rtype})


# ── Resource CRUD (existing) ────────────────────────────────────────────────

@router.get("/goals/{pid}/resources", response_class=HTMLResponse)
async def list_resources(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    rows = await pool.fetch(
        "SELECT * FROM resources WHERE goal_id = $1 AND status = 'active' ORDER BY added_at DESC",
        pid,
    )
    resources = [dict(r) for r in rows]

    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    # Agents bound to this goal
    agent_rows = await pool.fetch(
        """SELECT a.id, a.name, a.emoji
           FROM agent_contexts ac JOIN agents a ON a.id = ac.agent_id
           WHERE ac.goal_id = $1""",
        pid,
    )
    agents = [dict(r) for r in agent_rows]

    # Access grants grouped by resource (only fields needed by JS)
    access_rows = await pool.fetch(
        "SELECT resource_id, agent_id, access_level, constraints FROM resource_access WHERE goal_id = $1", pid,
    )
    access_map: dict[str, dict] = {}
    for r in access_rows:
        access_map.setdefault(r["resource_id"], {})[r["agent_id"]] = {
            "access_level": r["access_level"],
            "constraints": json.loads(r["constraints"]) if isinstance(r["constraints"], str) else (r["constraints"] or {}),
        }

    return templates.TemplateResponse(
        "resources/list.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "resources": resources,
            "role": role,
            "agents": agents,
            "access_map": access_map,
        },
    )


@router.post("/goals/{pid}/resources")
async def add_resource(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    form = await request.form()
    pool = get_pool()

    protected = [b.strip() for b in form.get("protected_branches", "").split(",") if b.strip()]

    rid = str(uuid.uuid4())[:12]
    name = form.get("name", "")
    rtype = form.get("type", "")
    uri = form.get("uri", "")
    default_access = form.get("default_access", "read")
    credentials_secret = form.get("credentials_secret", "")
    local_path = form.get("local_path", "")
    notes = form.get("notes", "")
    gcp_project = form.get("gcp_project", "")
    bucket = form.get("bucket", "")
    path_prefix = form.get("path_prefix", "")
    dataset = form.get("dataset", "")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=RESOURCE_ADDED,
                payload={
                    "resource_id": rid,
                    "goal_id": pid,
                    "name": name,
                    "type": rtype,
                    "uri": uri,
                    "default_access": default_access,
                    "protected_branches": protected,
                    "gcp_project": gcp_project,
                    "bucket": bucket,
                    "dataset": dataset,
                    "added_by": user["uid"],
                },
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )
            await conn.execute(
                """INSERT INTO resources
                       (id, goal_id, name, type, uri, default_access,
                        credentials_secret, local_path, protected_branches, notes, added_by,
                        gcp_project, bucket, path_prefix, dataset)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                rid, pid, name, rtype, uri, default_access,
                credentials_secret, local_path, protected, notes, user["uid"],
                gcp_project, bucket, path_prefix, dataset,
            )

    # Sync RESOURCES.md for all agents in this goal (new resource may affect defaults)
    try:
        await _sync_all_agents_in_goal(pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md for goal %s: %s", pid, exc)

    return RedirectResponse(url=f"/goals/{pid}/resources", status_code=303)


@router.get("/goals/{pid}/resources/{rid}")
async def get_resource(
    pid: str,
    rid: str,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM resources WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )
    if not row:
        raise HTTPException(status_code=404)
    return JSONResponse(dict(row), default=str)


@router.put("/goals/{pid}/resources/{rid}")
async def update_resource(
    pid: str,
    rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    body = await request.json()
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=RESOURCE_UPDATED,
                payload={
                    "resource_id": rid,
                    "goal_id": pid,
                    "changes": {k: v for k, v in body.items() if v is not None},
                },
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )
            await conn.execute(
                """UPDATE resources SET
                       name = COALESCE($1, name),
                       type = COALESCE($2, type),
                       uri = COALESCE($3, uri),
                       default_access = COALESCE($4, default_access),
                       credentials_secret = COALESCE($5, credentials_secret),
                       local_path = COALESCE($6, local_path),
                       notes = COALESCE($7, notes)
                   WHERE id = $8 AND goal_id = $9""",
                body.get("name"), body.get("type"), body.get("uri"),
                body.get("default_access"), body.get("credentials_secret"),
                body.get("local_path"), body.get("notes"),
                rid, pid,
            )

    try:
        await _sync_all_agents_in_goal(pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md for goal %s: %s", pid, exc)
    return JSONResponse({"ok": True})


@router.delete("/goals/{pid}/resources/{rid}")
async def delete_resource(
    pid: str,
    rid: str,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=RESOURCE_REMOVED,
                payload={"resource_id": rid, "goal_id": pid},
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )
            await conn.execute(
                "UPDATE resources SET status = 'deleted' WHERE id = $1 AND goal_id = $2",
                rid, pid,
            )

    try:
        await _sync_all_agents_in_goal(pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md for goal %s: %s", pid, exc)
    return JSONResponse({"ok": True})


# ── Per-agent access control ────────────────────────────────────────────────

@router.get("/goals/{pid}/resources/{rid}/access")
async def list_resource_access(
    pid: str,
    rid: str,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    rows = await pool.fetch(
        """SELECT ra.*, a.name as agent_name, a.emoji as agent_emoji
           FROM resource_access ra
           JOIN agents a ON a.id = ra.agent_id
           WHERE ra.resource_id = $1 AND ra.goal_id = $2""",
        rid, pid,
    )
    return JSONResponse([dict(r) for r in rows], default=str)


@router.post("/goals/{pid}/resources/{rid}/access")
async def grant_resource_access(
    pid: str,
    rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    form = await request.form()
    agent_id = form.get("agent_id", "")
    access_level = form.get("access_level", "read")
    constraints_raw = form.get("constraints", "{}")

    try:
        constraints = json.loads(constraints_raw)
    except (json.JSONDecodeError, TypeError):
        constraints = {}

    pool = get_pool()

    # Validate agent is bound to this goal
    bound = await pool.fetchval(
        "SELECT 1 FROM agent_contexts WHERE goal_id = $1 AND agent_id = $2",
        pid, agent_id,
    )
    if not bound:
        raise HTTPException(status_code=400, detail="Agent is not bound to this goal")

    # Fetch resource for branch protection logic
    resource = await pool.fetchrow(
        "SELECT * FROM resources WHERE id = $1 AND goal_id = $2", rid, pid,
    )
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    # For git_repo + read-write: auto-merge protected_branches into denied_branches
    if resource["type"] == "git_repo" and access_level == "read-write":
        protected = resource.get("protected_branches") or []
        if protected:
            denied = set(constraints.get("denied_branches", []))
            denied.update(protected)
            constraints["denied_branches"] = sorted(denied)

    # Check if grant already exists (for event type selection)
    existing = await pool.fetchrow(
        "SELECT * FROM resource_access WHERE resource_id = $1 AND agent_id = $2",
        rid, agent_id,
    )

    if existing:
        event_type = REGISTRY_ACCESS_UPDATED
        payload = {
            "resource_id": rid,
            "agent_id": agent_id,
            "goal_id": pid,
            "previous_access_level": existing["access_level"],
            "new_access_level": access_level,
            "previous_constraints": existing["constraints"] if isinstance(existing["constraints"], dict) else {},
            "new_constraints": constraints,
            "resource_name": resource["name"],
            "resource_type": resource["type"],
        }
    else:
        event_type = REGISTRY_ACCESS_GRANTED
        payload = {
            "resource_id": rid,
            "agent_id": agent_id,
            "goal_id": pid,
            "access_level": access_level,
            "constraints": constraints,
            "resource_name": resource["name"],
            "resource_type": resource["type"],
        }

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=event_type,
                payload=payload,
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )
            await conn.execute(
                """INSERT INTO resource_access
                       (resource_id, agent_id, goal_id, access_level, constraints, granted_by)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                   ON CONFLICT (resource_id, agent_id) DO UPDATE SET
                       access_level = $4,
                       constraints = $5::jsonb,
                       updated_at = now()""",
                rid, agent_id, pid, access_level,
                json.dumps(constraints), user["uid"],
            )

    # Sync RESOURCES.md for the affected agent (non-blocking — don't fail the request)
    try:
        await sync_resources_md(gateway_admin, agent_id, pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md after access grant for %s: %s", agent_id, exc)

    return RedirectResponse(url=f"/goals/{pid}/resources", status_code=303)


@router.delete("/goals/{pid}/resources/{rid}/access/{agent_id}")
async def revoke_resource_access(
    pid: str,
    rid: str,
    agent_id: str,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()

    # Fetch current grant for ledger payload
    existing = await pool.fetchrow(
        "SELECT * FROM resource_access WHERE resource_id = $1 AND agent_id = $2",
        rid, agent_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="No access grant found")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=REGISTRY_ACCESS_REVOKED,
                payload={
                    "resource_id": rid,
                    "agent_id": agent_id,
                    "goal_id": pid,
                    "previous_access_level": existing["access_level"],
                },
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )
            await conn.execute(
                "DELETE FROM resource_access WHERE resource_id = $1 AND agent_id = $2",
                rid, agent_id,
            )

    try:
        await sync_resources_md(gateway_admin, agent_id, pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md after access revoke for %s: %s", agent_id, exc)
    return JSONResponse({"ok": True})


# ── GitHub repo / branch listing ───────────────────────────────────────────

@router.get("/api/github/repos")
async def github_list_repos(user: dict = Depends(get_current_user)):
    """Return repos accessible to the GitHub App installation."""
    try:
        repos = await list_installation_repos()
        return JSONResponse(repos)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:
        _log.warning("Failed to list GitHub repos: %s", exc)
        return JSONResponse({"error": "Failed to fetch repos from GitHub"}, status_code=502)


@router.get("/api/github/repos/{owner}/{repo}/branches")
async def github_list_branches(
    owner: str,
    repo: str,
    user: dict = Depends(get_current_user),
):
    """Return branches for a specific repo."""
    try:
        branches = await list_repo_branches(f"{owner}/{repo}")
        return JSONResponse(branches)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:
        _log.warning("Failed to list branches for %s/%s: %s", owner, repo, exc)
        return JSONResponse({"error": "Failed to fetch branches from GitHub"}, status_code=502)


# ── Internal PEP API (gateway-facing) ───────────────────────────────────────

@router.get("/api/internal/pep/check")
async def pep_check(
    agent_id: str = Query(...),
    resource_id: str = Query(...),
    requested_level: str = Query(...),
    branch: str = Query(""),
    _key=Depends(require_internal_key),
):
    """Policy Enforcement Point — can agent X access resource Y at level Z?"""
    pool = get_pool()

    resource = await pool.fetchrow(
        "SELECT * FROM resources WHERE id = $1 AND status = 'active'", resource_id,
    )
    if not resource:
        return JSONResponse({"allowed": False, "reason": "resource_not_found"})

    # Check explicit grant
    grant = await pool.fetchrow(
        "SELECT * FROM resource_access WHERE resource_id = $1 AND agent_id = $2",
        resource_id, agent_id,
    )

    if grant:
        effective_level = grant["access_level"]
        source = "explicit_grant"
        grant_constraints = grant["constraints"] if isinstance(grant["constraints"], dict) else {}
    else:
        effective_level = resource["default_access"]
        source = "default_access"
        grant_constraints = {}

    # Check level hierarchy
    if ACCESS_LEVELS.get(effective_level, 0) < ACCESS_LEVELS.get(requested_level, 0):
        return JSONResponse({
            "allowed": False,
            "effective_level": effective_level,
            "reason": "access_denied",
            "source": source,
            "constraints": grant_constraints,
        })

    # Branch protection for git repos
    if (resource["type"] == "git_repo"
            and requested_level == "read-write"
            and branch):
        protected = set(resource.get("protected_branches") or [])
        denied = set(grant_constraints.get("denied_branches", []))
        all_denied = protected | denied

        if branch in all_denied:
            return JSONResponse({
                "allowed": False,
                "effective_level": effective_level,
                "reason": "branch_protected",
                "branch": branch,
                "denied_branches": sorted(all_denied),
                "source": source,
                "constraints": grant_constraints,
            })

    return JSONResponse({
        "allowed": True,
        "effective_level": effective_level,
        "reason": source,
        "constraints": grant_constraints,
    })


@router.get("/api/internal/pep/agent/{agent_id}/resources")
async def pep_agent_resources(
    agent_id: str,
    goal_id: str = Query(...),
    _key=Depends(require_internal_key),
):
    """Bulk query: all resources an agent can access in a goal with effective levels."""
    pool = get_pool()

    # All active resources in the goal
    resources = await pool.fetch(
        "SELECT * FROM resources WHERE goal_id = $1 AND status = 'active'", goal_id,
    )

    # Explicit grants for this agent
    grants = await pool.fetch(
        "SELECT * FROM resource_access WHERE agent_id = $1 AND goal_id = $2",
        agent_id, goal_id,
    )
    grant_map = {g["resource_id"]: dict(g) for g in grants}

    result = []
    for r in resources:
        rid = r["id"]
        grant = grant_map.get(rid)

        if grant:
            effective = grant["access_level"]
            source = "explicit"
            constraints = grant["constraints"] if isinstance(grant["constraints"], dict) else {}
        else:
            effective = r["default_access"]
            source = "default"
            constraints = {}

        if effective == "none":
            continue

        entry = {
            "resource_id": rid,
            "name": r["name"],
            "type": r["type"],
            "uri": r["uri"],
            "effective_level": effective,
            "source": source,
            "constraints": constraints,
        }
        if r["type"] == "git_repo":
            entry["protected_branches"] = r.get("protected_branches") or []

        result.append(entry)

    return JSONResponse(result, default=str)


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _sync_all_agents_in_goal(pid: str):
    """Sync RESOURCES.md for every agent bound to a goal."""
    pool = get_pool()
    agent_ids = await pool.fetch(
        "SELECT agent_id FROM agent_contexts WHERE goal_id = $1", pid,
    )
    for row in agent_ids:
        try:
            await sync_resources_md(gateway_admin, row["agent_id"], pid)
        except Exception as exc:
            _log.warning("Failed to sync RESOURCES.md for agent %s: %s", row["agent_id"], exc)
