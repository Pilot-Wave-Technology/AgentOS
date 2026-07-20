"""Internal API for SHCA memory — called by OpenClaw memory tools (HTTP-backed).

Endpoints:
  GET  /internal/memory/{agent_id}/search   — unified search across all tables
  POST /internal/memory/{agent_id}          — save to agent_memories
  GET  /internal/memory/{agent_id}/detail/{item_id} — drill-down with linked data
"""
import logging
from fastapi import Depends as _sec_Depends
from app.internal_auth import ownership_guard as _ownership_guard
from app.internal_auth import require_goal_ownership as _require_goal_ownership

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import get_pool
from app.memory import (
    search_memory,
    save_agent_memory,
    get_memory_detail,
    check_and_compact,
)

_log = logging.getLogger(__name__)
router = APIRouter(dependencies=[_sec_Depends(_ownership_guard)])


from app.internal_auth import verify_internal_bearer as _check_internal_auth  # SSOT: app/internal_auth.py


async def _resolve_instance_context(pool, agent_id: str) -> dict:
    """Resolve goal_id, thread_id, and template_id from a gateway agent ID.

    The agent_id can be either:
    - A gateway instance ID (e.g. "abhishek-shca-2ddca454-3b1b06c2-...")
      → look up in agent_instances table
    - A template ID (e.g. "abhishek-shca")
      → return as-is, no goal/thread resolution
    """
    # Try agent_instances first (SHCA per-conversation instances)
    row = await pool.fetchrow("""
        SELECT template_id, goal_id, thread_id
        FROM agent_instances
        WHERE gateway_agent_id = $1 AND status = 'active'
        LIMIT 1
    """, agent_id)

    if row:
        return {
            "template_id": row["template_id"],
            "goal_id": row["goal_id"] or "",
            "thread_id": row["thread_id"] or "",
        }

    # Fall back: agent_id might be a template ID directly
    return {
        "template_id": agent_id,
        "goal_id": "",
        "thread_id": "",
    }


# ── Search ───────────────────────────────────────────────────────────────────

@router.get("/internal/memory/{agent_id}/search")
async def memory_search_endpoint(
    agent_id: str,
    request: Request,
    q: str = Query("", description="Search query"),
    goal_id: str = Query("", description="Current goal ID"),
    thread_id: str = Query("", description="Current thread ID"),
    scope: str = Query("all", description="all | goal | conversation | cross_goal"),
    type: str = Query(None, description="decision | task_outcome | blocker | preference | etc"),
    max_results: int = Query(10, ge=1, le=50),
):
    """Unified memory search — fans out across ledger, tasks, and agent_memories."""
    _check_internal_auth(request)

    pool = get_pool()

    # Resolve template_id, goal_id, thread_id from the agent instance
    ctx = await _resolve_instance_context(pool, agent_id)
    template_id = ctx["template_id"]
    resolved_goal_id = goal_id or ctx["goal_id"]
    resolved_thread_id = thread_id or ctx["thread_id"]

    _log.info("memory_search: agent=%s template=%s q='%s' scope=%s type=%s goal=%s thread=%s",
              agent_id, template_id, q[:50], scope, type,
              resolved_goal_id[:12] if resolved_goal_id else "",
              resolved_thread_id[:12] if resolved_thread_id else "")

    if not q:
        return JSONResponse({"results": [], "query": q})

    try:
        results = await search_memory(
            pool,
            agent_template_id=template_id,
            goal_id=resolved_goal_id,
            thread_id=resolved_thread_id,
            q=q,
            scope=scope,
            memory_type=type,
            max_results=max_results,
        )
        _log.info("memory_search: %d results for '%s'", len(results), q[:50])
        return JSONResponse({"results": results, "query": q, "count": len(results)})
    except Exception as exc:
        _log.error("memory_search failed: %s", exc, exc_info=True)
        return JSONResponse({"results": [], "query": q, "error": str(exc)}, status_code=500)


# ── Save ─────────────────────────────────────────────────────────────────────

@router.post("/internal/memory/{agent_id}")
async def memory_save_endpoint(agent_id: str, request: Request):
    """Save a memory to agent_memories. Called by SHCA via memory_save tool."""
    _check_internal_auth(request)

    body = await request.json()
    pool = get_pool()

    # Resolve context from instance
    ctx = await _resolve_instance_context(pool, agent_id)
    template_id = ctx["template_id"]
    _log.info("memory_save: agent=%s template=%s title='%s' type=%s scope=%s",
              agent_id, template_id, body.get("title", "")[:50], body.get("type"), body.get("scope"))

    # Required fields
    title = body.get("title", "").strip()
    content = body.get("content", "").strip()
    memory_type = body.get("type", "").strip()
    scope = body.get("scope", "goal").strip()

    if not title or not content or not memory_type:
        raise HTTPException(400, "title, content, and type are required")

    if memory_type not in ("preference", "observation", "context", "convention"):
        raise HTTPException(400, f"Invalid type: {memory_type}")

    if scope not in ("global", "goal", "conversation"):
        raise HTTPException(400, f"Invalid scope: {scope}")

    goal_id = body.get("goal_id", "").strip() or ctx["goal_id"]
    _require_goal_ownership(request, goal_id)  # #9: body goal_id not seen by ownership_guard
    thread_id = body.get("thread_id", "").strip() or ctx["thread_id"]

    if scope in ("goal", "conversation") and not goal_id:
        raise HTTPException(400, "goal_id required for goal/conversation scope (could not resolve from instance)")
    if scope == "conversation" and not thread_id:
        raise HTTPException(400, "thread_id required for conversation scope (could not resolve from instance)")

    # Resolve SHCA template info
    shca = await pool.fetchrow(
        "SELECT name, owner_actor_id FROM agents WHERE id = $1", template_id,
    )
    if not shca:
        raise HTTPException(404, f"Agent template {template_id} not found")

    # Resolve human info — prefer thread creator, fall back to SHCA owner
    human_uid = ""
    human_name = ""

    if thread_id:
        thread = await pool.fetchrow(
            "SELECT created_by FROM threads WHERE id = $1", thread_id,
        )
        if thread and thread["created_by"]:
            human_uid = thread["created_by"]
            user = await pool.fetchrow(
                "SELECT display_name FROM users WHERE uid = $1", human_uid,
            )
            human_name = user["display_name"] if user else ""

    if not human_name and shca["owner_actor_id"]:
        # Fall back to SHCA owner (actor_id is UUID, look up by actor_id)
        user = await pool.fetchrow(
            "SELECT uid, display_name FROM users WHERE actor_id = $1",
            shca["owner_actor_id"],
        )
        if user:
            human_uid = user["uid"]
            human_name = user["display_name"] or ""

    memory_id = await save_agent_memory(
        pool,
        agent_template_id=template_id,
        agent_template_name=shca["name"],
        scope=scope,
        memory_type=memory_type,
        title=title,
        content=content,
        goal_id=goal_id or None,
        thread_id=thread_id or None,
        human_uid=human_uid or None,
        human_name=human_name or None,
        source="agent",
        related_files=body.get("files", []),
        tags=body.get("tags", []),
    )

    # Check compaction
    await check_and_compact(
        pool, template_id, goal_id or None, scope, thread_id or None,
    )

    # Refresh MEMORY.md on active instance (best effort)
    if goal_id and thread_id:
        try:
            await _refresh_active_instance(pool, template_id, goal_id, thread_id)
        except Exception as exc:
            _log.warning("Failed to refresh MEMORY.md after save: %s", exc)

    _log.info("memory_save: saved id=%s title='%s'", memory_id, title[:50])
    return JSONResponse({"ok": True, "id": memory_id})


# ── Detail ───────────────────────────────────────────────────────────────────

@router.get("/internal/memory/{agent_id}/detail/{item_id}")
async def memory_detail_endpoint(
    agent_id: str,
    item_id: str,
    request: Request,
    source: str = Query(None, description="ledger | tasks | memory (auto-detected if omitted)"),
):
    """Get full detail for a memory search result — drill-down with linked data."""
    _check_internal_auth(request)

    _log.info("memory_detail: agent=%s item=%s source=%s", agent_id, item_id, source)

    pool = get_pool()
    try:
        detail = await get_memory_detail(pool, item_id, source)
    except Exception as exc:
        _log.error("memory_detail failed for %s: %s", item_id, exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)

    if not detail:
        _log.warning("memory_detail: item %s not found", item_id)
        raise HTTPException(404, f"Item {item_id} not found")

    _log.info("memory_detail: returned %s for %s", detail.get("source", "?"), item_id)
    return JSONResponse(_serialize(detail))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _serialize(obj):
    """Recursively convert non-JSON-serializable types."""
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    # UUID
    if hasattr(obj, "hex") and hasattr(obj, "int"):
        return str(obj)
    return obj


async def _refresh_active_instance(
    pool, agent_template_id: str, goal_id: str, thread_id: str,
) -> None:
    """Regenerate and write MEMORY.md to the active instance for this thread."""
    from app.memory import generate_memory_md
    from app.openclaw import gateway_admin

    # Find active instance for this thread
    inst = await pool.fetchrow("""
        SELECT gateway_agent_id FROM agent_instances
        WHERE template_id = $1 AND thread_id = $2 AND status = 'active'
        LIMIT 1
    """, agent_template_id, thread_id)

    if not inst:
        return

    md = await generate_memory_md(pool, agent_template_id, goal_id, thread_id)
    await gateway_admin.set_agent_file(inst["gateway_agent_id"], "MEMORY.md", md)
    _log.info("Refreshed MEMORY.md for instance %s", inst["gateway_agent_id"])
