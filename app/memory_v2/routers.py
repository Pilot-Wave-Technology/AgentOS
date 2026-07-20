"""HTTP routers for the v2 memory system.

Single surface — role-based view at one consistent URL:

  /settings/memory/prompts            admin: edit + version history
                                       other: read-only view

The bulk of the work is GET/POST against the `memory_prompts` table;
all real DB work lives in app/memory_v2/prompts.py.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, require_admin
from app.roles import SystemRole
from app.database import get_pool
from app.flash import flash_redirect
from app.memory_v2 import prompts as prompts_api
from app.ledger.actors import human_actor


_log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ── Unified GET views — role-based content at one URL ──────────────────────

@router.get("/settings/memory/prompts", response_class=HTMLResponse)
async def prompts_list(request: Request, user: dict = Depends(require_admin)):
    """List all active prompts. Restricted to platform.admin (require_admin)."""
    all_prompts = await prompts_api.get_all_active_prompts()
    by_agent: dict[str, dict[str, str]] = {"SHCA": {}, "GO": {}}
    for (agent_type, kind), content in all_prompts.items():
        by_agent.setdefault(agent_type, {})[kind] = content
    return templates.TemplateResponse(
        "memory_v2/admin_prompts.html",
        {"request": request, "user": user, "by_agent": by_agent},
    )


@router.get("/settings/memory/prompts/{agent_type}/{prompt_kind}", response_class=HTMLResponse)
async def prompt_detail(
    agent_type: str,
    prompt_kind: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Single-prompt view. Admins see the edit form; others see read-only content."""
    is_admin = user.get("system_role") == SystemRole.PLATFORM_ADMIN
    try:
        if is_admin:
            active_content = await prompts_api.get_active_prompt(agent_type, prompt_kind)
            versions = await prompts_api.list_versions(agent_type, prompt_kind, limit=10)
            return templates.TemplateResponse(
                "memory_v2/admin_prompt_edit.html",
                {
                    "request": request,
                    "user": user,
                    "agent_type": agent_type,
                    "prompt_kind": prompt_kind,
                    "active_content": active_content or "",
                    "versions": versions,
                },
            )
        content = await prompts_api.get_active_prompt(agent_type, prompt_kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if content is None:
        raise HTTPException(status_code=404, detail="no active prompt for this slot")
    return templates.TemplateResponse(
        "memory_v2/prompt_single.html",
        {
            "request": request,
            "user": user,
            "agent_type": agent_type,
            "prompt_kind": prompt_kind,
            "content": content,
            "is_admin": False,
        },
    )


# Note: activate route MUST be registered before the save route below so
# that `/settings/memory/prompts/activate/{uuid}` doesn't get matched by the
# more-general `/settings/memory/prompts/{agent_type}/{prompt_kind}` pattern.
# Both are POST + two-segment paths; FastAPI matches by registration order.
@router.post("/settings/memory/prompts/activate/{prompt_id}")
async def admin_prompt_activate(
    prompt_id: uuid.UUID,
    user: dict = Depends(require_admin),
):
    # The admin performing this action is the actor (system if uid unresolved).
    actor_id, _ = await human_actor(user["uid"])
    try:
        row = await prompts_api.activate_version(prompt_id, actor_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return RedirectResponse(
        url=f"/settings/memory/prompts/{row['agent_type']}/{row['prompt_kind']}",
        status_code=303,
    )


@router.post("/settings/memory/prompts/{agent_type}/{prompt_kind}")
async def admin_prompt_save(
    agent_type: str,
    prompt_kind: str,
    content: str = Form(...),
    notes: str = Form(""),
    activate: bool = Form(False),
    user: dict = Depends(require_admin),
):
    # The admin performing this action is the actor (system if uid unresolved).
    actor_id, _ = await human_actor(user["uid"])
    try:
        row = await prompts_api.save_new_version(
            agent_type=agent_type,
            prompt_kind=prompt_kind,
            content=content,
            created_by=actor_id,
            notes=notes or None,
            activate=activate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return flash_redirect(
        f"/settings/memory/prompts/{agent_type}/{prompt_kind}",
        f"Saved as version {row['version']}.",
    )


@router.post("/settings/memory/prompts/repush/{agent_id}")
async def admin_prompt_repush(
    agent_id: str,
    agent_type: str = Form(...),
    user: dict = Depends(require_admin),
):
    """Re-push the currently-active compaction + flush prompts to an EXISTING
    gateway agent. Prompts are normally pushed only at agent creation (there
    is no re-push on resume), so editing a prompt or shipping a new prompt
    migration never reaches agents that already exist. This forces a re-push
    so a live agent picks up the latest active versions.
    """
    agent_type = (agent_type or "").strip().upper()
    ok = await prompts_api.apply_prompts_to_new_agent(
        agent_id=agent_id, agent_type=agent_type,
    )
    return JSONResponse({
        "ok": ok,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "reason": "re-pushed active prompts"
        if ok
        else "no active prompts for this type, or gateway push failed (see logs)",
    })


@router.post("/settings/memory/prompts/repush-all")
async def admin_prompt_repush_all(
    agent_type: str = Form(""),
    user: dict = Depends(require_admin),
):
    """Re-push the active compaction + flush prompts to every active SHCA/GO
    agent whose GOAL is still active.

    Scope (what gets touched — nothing else): an agent is re-pushed only when it
    is itself active AND it belongs to a goal with goals.status='active' (not
    soft-deleted). Agents for deleted goals are skipped — that's the source of the
    stale "agent not found" gateway rows, so this scoping removes that noise.

    Prompts are pushed only at agent creation (no re-push on resume), so a new
    prompt migration never reaches agents that already exist. This bulk endpoint
    re-pushes the active versions so a freshly-migrated prompt lands everywhere in
    one call. Idempotent and safe to re-run.

    IMPORTANT — two instance models, two sources (UNIONed below):
      * SHCA instances live in `agent_instances` (status='active', gateway_agent_id,
        template_id -> the SHCA template in `agents`).
      * GO instances live in the `agents` table itself: a per-goal row with
        is_template=FALSE, type='GO', status='active', gateway_agent_id='go-<goal>'
        (see go_agent.py). They are NOT in agent_instances — an agent_instances-only
        query silently misses every GO (the 2026-06-12 bug this UNION fixes).

    Optional `agent_type` form field ('SHCA' or 'GO') restricts the sweep; empty
    means both. Best-effort per agent — one push failing does not abort the rest.
    """
    wanted = (agent_type or "").strip().upper()
    if wanted and wanted not in ("SHCA", "GO"):
        raise HTTPException(status_code=400, detail="agent_type must be SHCA, GO, or empty")
    types = [wanted] if wanted else ["SHCA", "GO"]

    pool = get_pool()
    # UNION both instance models, scoped to agents whose GOAL is still active
    # (goals.status='active', i.e. not soft-deleted — the planning/paused/etc.
    # lifecycle lives in a separate column; per the "check status not state"
    # rule we gate on status). This intentionally excludes agents for deleted
    # goals, which is where the stale "agent not found" gateway rows come from.
    #   * SHCAs: agent_instances (active) -> goal via ai.goal_id.
    #   * GOs:   non-template `agents` row (active) -> goal via agent_contexts
    #            (agent_contexts.agent_id REFERENCES agents.id; for a GO,
    #             agents.id == gateway_agent_id == 'go-<goal>').
    rows = await pool.fetch(
        """
        SELECT ai.gateway_agent_id, a.type
          FROM agent_instances ai
          JOIN agents a  ON a.id = ai.template_id
          JOIN goals  gl ON gl.id = ai.goal_id
         WHERE ai.status = 'active'
           AND gl.status = 'active'
           AND a.type = ANY($1::text[])
           AND ai.gateway_agent_id IS NOT NULL
        UNION
        SELECT ga.gateway_agent_id, ga.type
          FROM agents ga
          JOIN agent_contexts ac ON ac.agent_id = ga.id
          JOIN goals          gl ON gl.id = ac.goal_id
         WHERE ga.is_template = FALSE
           AND ga.status = 'active'
           AND gl.status = 'active'
           AND ga.type = ANY($1::text[])
           AND ga.gateway_agent_id IS NOT NULL
        """,
        types,
    )
    # Tally what the enumeration found, by type, before pushing — so a "0 GOs"
    # coverage gap is visible in the logs rather than silent.
    by_type_found: dict[str, int] = {}
    for r in rows:
        by_type_found[r["type"]] = by_type_found.get(r["type"], 0) + 1
    _log.info("repush-all: enumerated %d agent(s) by type=%s (requested types=%s)",
              len(rows), by_type_found, types)

    pushed: list[dict] = []
    failed: list[dict] = []
    # De-dup gateway agent ids (an agent could appear in both sources).
    seen: set[str] = set()
    for r in rows:
        gw_id = r["gateway_agent_id"]
        a_type = r["type"]
        if not gw_id or gw_id in seen:
            continue
        seen.add(gw_id)
        try:
            ok = await prompts_api.apply_prompts_to_new_agent(
                agent_id=gw_id, agent_type=a_type,
            )
        except Exception as exc:  # noqa: BLE001 — report, don't abort the sweep
            _log.exception("repush-all: push raised for %s (type=%s)", gw_id, a_type)
            failed.append({"agent_id": gw_id, "agent_type": a_type, "error": str(exc)})
            continue
        (pushed if ok else failed).append({"agent_id": gw_id, "agent_type": a_type})

    pushed_by_type: dict[str, int] = {}
    for p in pushed:
        pushed_by_type[p["agent_type"]] = pushed_by_type.get(p["agent_type"], 0) + 1
    _log.info(
        "repush-all: pushed=%d (by type=%s) failed=%d types=%s",
        len(pushed), pushed_by_type, len(failed), types,
    )
    return JSONResponse({
        "ok": len(failed) == 0,
        "types": types,
        "total": len(seen),
        "found_by_type": by_type_found,
        "pushed_by_type": pushed_by_type,
        "pushed_count": len(pushed),
        "failed_count": len(failed),
        "pushed": pushed,
        "failed": failed,
    })


# ── Manual triggers (chat UI buttons) ──────────────────────────────────────
# Two endpoints any chat page can call to force a memory action on demand —
# bypass the natural triggers so testing doesn't require waiting for token
# thresholds / story completions. Both work on whichever (agent, thread) the
# caller passes.

@router.post("/api/memory/flush")
async def trigger_manual_flush_endpoint(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Fire a silent flush turn on a specific agent + thread.
    Body: form-encoded { goal_id, agent_id, thread_id, agent_type }.
    """
    form = await request.form()
    goal_id = (form.get("goal_id") or "").strip()
    agent_id = (form.get("agent_id") or "").strip()
    thread_id = (form.get("thread_id") or "").strip()
    agent_type = (form.get("agent_type") or "SHCA").strip().upper()

    if not goal_id or not agent_id or not thread_id:
        raise HTTPException(status_code=400, detail="goal_id, agent_id, thread_id required")

    # Permission: caller must be a member of the goal.
    from app.auth import get_goal_role
    role = await get_goal_role(user["uid"], goal_id)
    if role is None:
        raise HTTPException(status_code=403, detail="not a goal member")

    from app.memory_v2 import flush as _flush
    ok = await _flush.trigger_manual_flush(
        agent_id=agent_id, agent_type=agent_type,
        thread_id=thread_id, goal_id=goal_id,
    )
    return JSONResponse({
        "ok": ok,
        "flush_type": "manual",
        "reason": "sent" if ok else "no live session — open the chat first",
    })


@router.post("/api/memory/compact")
async def trigger_manual_compact_endpoint(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Force OpenClaw to compact the session — prunes oldest turns,
    bumps compactionCount. Different from memory flush: this is the
    native context-window trim, not the agent writing memory.
    """
    form = await request.form()
    goal_id = (form.get("goal_id") or "").strip()
    agent_id = (form.get("agent_id") or "").strip()
    thread_id = (form.get("thread_id") or "").strip()

    if not goal_id or not agent_id:
        raise HTTPException(status_code=400, detail="goal_id, agent_id required")

    from app.auth import get_goal_role
    role = await get_goal_role(user["uid"], goal_id)
    if role is None:
        raise HTTPException(status_code=403, detail="not a goal member")

    # Resolve template_id (chat URL) to the actual gateway agent id, so
    # the session key matches what the gateway stored.
    gateway_agent_id = await _resolve_gateway_agent_id(agent_id, goal_id, thread_id)
    session_key = (
        f"agent:{gateway_agent_id}:{goal_id.lower()}-{thread_id}"
        if thread_id else f"agent:{gateway_agent_id}:"
    )

    # Manual button now invokes Pi's compaction pathway (sessions.piCompact),
    # which fires the safeguard with our per-agent summaryPrompt + LLM
    # summarization. Replaces the legacy line-truncation sessions.compact
    # path. Pi compaction preserves recent turns + the pending user
    # message — nothing gets discarded by clicking compact.
    from app.openclaw import gateway_admin, GatewayError
    try:
        result = await gateway_admin.call(
            "sessions.piCompact",
            {"key": session_key},
            timeout=120,  # LLM summarization can take a while
        )
        return JSONResponse({
            "ok": True,
            "compacted": bool(result.get("compacted")),
            "result": result,
        })
    except GatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"compact failed: {exc}")


@router.get("/api/memory/usage")
async def get_session_usage_endpoint(
    goal_id: str,
    agent_id: str,
    thread_id: str = "",
    user: dict = Depends(get_current_user),
):
    """Return the live context-usage snapshot for one (goal, agent, thread).

    Powers the chat-header usage ring. Cheap — reads SessionEntry counters
    the gateway already maintains; no transcript scan.
    """
    if not goal_id or not agent_id:
        raise HTTPException(status_code=400, detail="goal_id, agent_id required")

    from app.auth import get_goal_role
    role = await get_goal_role(user["uid"], goal_id)
    if role is None:
        raise HTTPException(status_code=403, detail="not a goal member")

    # Resolve template_id → gateway agent id (same fix as compact).
    gateway_agent_id = await _resolve_gateway_agent_id(agent_id, goal_id, thread_id)
    session_key = (
        f"agent:{gateway_agent_id}:{goal_id.lower()}-{thread_id}"
        if thread_id else f"agent:{gateway_agent_id}:"
    )

    from app.openclaw import gateway_admin, GatewayError
    try:
        result = await gateway_admin.call(
            "sessions.status", {"key": session_key}, timeout=10,
        )
        # Pass through verbatim — small payload, browser does the math.
        return JSONResponse(result)
    except GatewayError as exc:
        # Most likely cause: session not yet live. Return empty payload
        # rather than 502 so the UI just shows "—".
        _log.info("sessions.status failed for %s: %s", session_key, exc)
        return JSONResponse({"ok": False, "key": session_key, "reason": str(exc)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"usage fetch failed: {exc}")


def _looks_like_uuid(s) -> bool:
    if not isinstance(s, str):
        return False
    try:
        uuid.UUID(s)
        return True
    except Exception:
        return False


async def _resolve_gateway_agent_id(
    agent_id: str, goal_id: str, thread_id: str,
) -> str:
    """Map a chat-URL agent id to the actual gateway agent id.

    Both SHCA and GO are template→instance: chat URLs carry the template
    id (SHCA: "abhishek-shca"; GO: "go-<goal>") but the gateway addresses
    the per-instance gateway_agent_id from agent_instances. For SHCA the
    two differ (instance gets a goal+uuid suffix); for GO they currently
    match — but only by coincidence. Always resolve through
    agent_instances; don't special-case either type.

    Falls back to the input unchanged if no active agent_instances row
    matches — defensive only, indicates a provisioning gap.
    """
    try:
        from app.database import get_pool
        pool = get_pool()
        # The agent's LIVE session runs under its RAW instance id — the platform
        # records that verbatim on the thread (threads.instance_id) and builds
        # every session key from it (session._gateway_agent_id = instance_id).
        # That string is deliberately NOT the same as agent_instances.gateway_
        # agent_id, which stores the gateway-NORMALIZED id (lowercased + truncated
        # to 64 chars) because token_calls.agent_id echoes the normalized form for
        # cost joins. But the gateway persists the session ENTRY under the raw key
        # (toAgentStoreSessionKey keeps the key's agent part verbatim), so for the
        # usage/compact session key we MUST use the raw id — otherwise
        # sessions.status looks under a key the gateway never created and the chat
        # context-usage ring comes up empty (only worked before when a row happened
        # to still hold the raw id). Prefer threads.instance_id; fall back to the
        # agent_instances lookups below for GO / legacy rows with no thread instance.
        if thread_id:
            # Guard on the agent prefix so a shared/handed-off thread can only
            # ever return THIS agent's instance (instance_id is built as
            # f"{agent_id}-{goal_id}-{thread_id}", so its prefix is agent_id).
            inst = await pool.fetchval(
                """SELECT instance_id FROM threads
                    WHERE id = $1 AND starts_with(instance_id, $2 || '-')""",
                thread_id, agent_id,
            )
            if inst:
                return inst
        row = await pool.fetchrow(
            """
            SELECT gateway_agent_id
              FROM agent_instances
             WHERE template_id = $1
               AND thread_id = $2
               AND status = 'active'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            agent_id, thread_id,
        )
        if row and row["gateway_agent_id"]:
            return row["gateway_agent_id"]
        # Fallback: try by goal alone (covers SHCAs not yet bound to a
        # specific thread).
        if goal_id:
            row = await pool.fetchrow(
                """
                SELECT gateway_agent_id
                  FROM agent_instances
                 WHERE template_id = $1
                   AND LOWER(goal_id) = LOWER($2)
                   AND status = 'active'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                agent_id, goal_id,
            )
            if row and row["gateway_agent_id"]:
                return row["gateway_agent_id"]
    except Exception:
        _log.exception("_resolve_gateway_agent_id failed for %s", agent_id)
    return agent_id
