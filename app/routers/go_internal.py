"""Internal API endpoints for GO agent tools.

These endpoints are called by OpenClaw tools (resource_summary, etc.)
using Bearer token auth (same as existing agent callback endpoints).
"""
import json
from fastapi import Depends as _sec_Depends
from app.internal_auth import ownership_guard as _ownership_guard
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import get_settings
from app.database import get_pool, resolve_goal_id
from app.services.resource_scanner import (
    get_resource_summary,
    scan_resource,
    read_repo_file,
)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/go", dependencies=[_sec_Depends(_ownership_guard)])


from app.internal_auth import verify_internal_bearer as _verify_auth  # SSOT: app/internal_auth.py


# ── Resource Summary (read from DB) ─────────────────────────────────────────

@router.get("/{goal_id}/resources/{resource_id}/summary")
async def resource_summary(goal_id: str, resource_id: str, request: Request):
    """Get stored summary for a resource. Fast — reads from DB only."""
    _verify_auth(request)

    goal_id = await resolve_goal_id(goal_id)
    summary = await get_resource_summary(resource_id)
    if not summary:
        # No summary yet — return basic resource info
        pool = get_pool()
        resource = await pool.fetchrow(
            "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2",
            resource_id, goal_id,
        )
        if not resource:
            return JSONResponse({"error": "Resource not found"}, status_code=404)

        return JSONResponse({
            "resource_name": resource["name"],
            "resource_kind": resource["kind"],
            "summary": "No summary available. Use resource_scan to generate one.",
            "is_stale": True,
        })

    return JSONResponse(summary)


# ── Resource Scan (trigger fresh analysis) ───────────────────────────────────

@router.post("/{goal_id}/resources/{resource_id}/scan")
async def resource_scan(goal_id: str, resource_id: str, request: Request):
    """Trigger a fresh scan of a resource. Slow — fetches from GitHub/GCS + LLM."""
    _verify_auth(request)
    goal_id = await resolve_goal_id(goal_id)

    try:
        result = await scan_resource(goal_id, resource_id)
        return JSONResponse(result)
    except ValueError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        return JSONResponse({"error": str(exc)}, status_code=status)
    except Exception as exc:
        _log.error("Resource scan failed: %s/%s: %s", goal_id, resource_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Resource Read (read specific file from repo) ────────────────────────────

@router.get("/{goal_id}/resources/{resource_id}/read")
async def resource_read(
    goal_id: str, resource_id: str, request: Request,
    path: str = "", lines: int = 500,
):
    """Read a file: from repo via GitHub API, or uploaded document from GCS."""
    _verify_auth(request)

    goal_id = await resolve_goal_id(goal_id)
    pool = get_pool()
    resource = await pool.fetchrow(
        "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2",
        resource_id, goal_id,
    )
    if not resource:
        return JSONResponse({"error": "Resource not found"}, status_code=404)

    # For uploaded documents — return extracted text from GCS
    if resource["kind"] in ("document", "file") and resource.get("gcs_uri"):
        try:
            from app.services.resource_scanner import _download_from_gcs, _extract_pdf, _extract_excel, _extract_word
            data = await _download_from_gcs(resource["gcs_uri"])
            if not data:
                return JSONResponse({"error": "Failed to download file from storage"}, status_code=500)

            file_type = resource.get("file_type", "")
            if file_type in ("md", "txt", "markdown", "csv", "json", "yaml", "yml"):
                content = data.decode("utf-8", errors="replace")
            elif file_type == "pdf":
                result = await _extract_pdf(data, resource["name"])
                content = result.get("text", result.get("error", "Extraction failed"))
            elif file_type in ("xlsx", "xls"):
                result = await _extract_excel(data, resource["name"])
                content = result.get("text", result.get("error", "Extraction failed"))
            elif file_type in ("docx", "doc"):
                result = await _extract_word(data, resource["name"])
                content = result.get("text", result.get("error", "Extraction failed"))
            else:
                return JSONResponse({"error": f"Cannot read file type: {file_type}"}, status_code=400)

            # Trim to line limit
            content_lines = content.split("\n")
            truncated = len(content_lines) > lines
            if truncated:
                content = "\n".join(content_lines[:lines]) + f"\n\n... truncated ({len(content_lines)} total lines)"

            return JSONResponse({
                "path": resource["name"],
                "content": content,
                "size": resource.get("file_size_bytes", len(data)),
                "truncated": truncated,
                "total_lines": len(content_lines),
                "file_type": file_type,
            })
        except Exception as exc:
            return JSONResponse({"error": f"Failed to read document: {exc}"}, status_code=500)

    # For repo/branch — read via GitHub API
    if resource["kind"] not in ("repo", "branch"):
        return JSONResponse({"error": f"Read not supported for resource kind: {resource['kind']}"}, status_code=400)

    if not path:
        return JSONResponse({"error": "path parameter required for repo/branch resources"}, status_code=400)

    # For branches, get parent repo info
    repo_owner = resource.get("repo_owner", "")
    repo_name = resource.get("repo_name", "")
    branch = resource.get("default_branch", "main")

    if resource["kind"] == "branch":
        parent = await pool.fetchrow(
            "SELECT * FROM goal_resources WHERE id = $1", resource.get("parent_id", ""),
        )
        if parent:
            repo_owner = parent.get("repo_owner", "")
            repo_name = parent.get("repo_name", "")
        branch = resource.get("branch_name", branch)

    if not repo_owner or not repo_name:
        return JSONResponse({"error": "Repo owner/name not configured"}, status_code=400)

    github_token = await _get_github_token(goal_id)

    result = await read_repo_file(
        repo_owner, repo_name, path, branch, github_token, max_lines=lines,
    )

    return JSONResponse(result)


# ── List resources for a goal ────────────────────────────────────────────────

@router.get("/{goal_id}/resources")
async def list_resources(goal_id: str, request: Request):
    """List all resources for a goal with summary status."""
    _verify_auth(request)

    goal_id = await resolve_goal_id(goal_id)
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT gr.id, gr.kind, gr.name, gr.uri, gr.parent_id,
               gr.locked_by, gr.locked_for, gr.status,
               gr.last_scanned_at, gr.last_summary_at, gr.summary_stale,
               gr.file_type, gr.human_context,
               gr.repo_owner, gr.repo_name, gr.branch_name
        FROM goal_resources gr
        WHERE gr.goal_id = $1 AND gr.status = 'active'
        ORDER BY gr.kind, gr.name
    """, goal_id)

    resources = []
    for r in rows:
        res = dict(r)
        # Format timestamps
        for ts_field in ("last_scanned_at", "last_summary_at"):
            if res.get(ts_field):
                res[ts_field] = res[ts_field].isoformat()
        resources.append(res)

    return JSONResponse({"resources": resources})


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_github_token(goal_id: str, repo_name: str = "", write: bool = True) -> str:
    """Get GitHub installation token scoped to a repo.

    Defaults to read-write contents permission so GO-side operations like
    branch creation and commits succeed. Pass write=False for read-only
    use cases (file inspection, listing).
    """
    try:
        from app.github_tokens import generate_installation_token

        if not repo_name:
            # Find repo names from goal resources
            pool = get_pool()
            repos = await pool.fetch("""
                SELECT DISTINCT repo_name FROM goal_resources
                WHERE goal_id = $1 AND repo_name != '' AND status = 'active'
            """, goal_id)
            repo_names = [r["repo_name"] for r in repos if r["repo_name"]]
        else:
            repo_names = [repo_name]

        if not repo_names:
            _log.warning("No repos found for goal %s — token will be unscoped", goal_id)
            repo_names = []

        contents_perm = "write" if write else "read"
        _log.info(
            "_get_github_token: goal=%s repos=%s contents=%s",
            goal_id, repo_names, contents_perm,
        )
        result = await generate_installation_token(
            repo_names=repo_names,
            permissions={"contents": contents_perm, "metadata": "read", "workflows": contents_perm},
        )
        return result.get("token", "") if isinstance(result, dict) else (result or "")
    except Exception as exc:
        _log.warning("GitHub token generation failed for goal %s: %s", goal_id, exc)
        return ""
