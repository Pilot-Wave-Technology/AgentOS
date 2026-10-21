"""Orchestrator channel — GO↔Assistant coordination thread.

A dedicated thread per goal where assistants and the GO communicate.
Humans can observe (read-only) but cannot post messages.
Messages are attributed to the story each assistant is working on.
"""
import json
from fastapi import Depends as _sec_Depends
from app.internal_auth import ownership_guard as _ownership_guard
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role, verify_firebase_token
from app.config import get_settings
from app.internal_auth import verify_internal_bearer
from app.database import get_pool, resolve_goal_id
from app.openclaw import session_manager, gateway_admin
from app.services.go_agent import build_go_context
from app.routers.thread_paging import fetch_channel_page, register_channel_earlier_route

_log = logging.getLogger(__name__)

# NOTE: the router-level ownership_guard applies to every route registered
# below, including the paging route added by register_channel_earlier_route().
router = APIRouter(dependencies=[_sec_Depends(_ownership_guard)])
templates = Jinja2Templates(directory="app/templates")

# Visual identity for this channel's card chrome, passed to the shared
# channels/_message.html partial. Plain rows override it per-role via
# data-accent (emerald for GO, indigo for assistants, gray for system).
_ACCENT = "amber"

# ── Orchestrator channel subscriber registry ──────────────────────────────────
_orch_subscribers: dict[str, set] = {}


def _subscribe_to_orchestrator(goal_id: str, send_fn):
    if goal_id not in _orch_subscribers:
        _orch_subscribers[goal_id] = set()
    _orch_subscribers[goal_id].add(send_fn)

    def unsubscribe():
        _orch_subscribers.get(goal_id, set()).discard(send_fn)
        if goal_id in _orch_subscribers and not _orch_subscribers[goal_id]:
            del _orch_subscribers[goal_id]

    return unsubscribe


async def _broadcast_to_orchestrator(goal_id: str, message: str):
    subscribers = _orch_subscribers.get(goal_id, set()).copy()
    for send_fn in subscribers:
        try:
            await send_fn(message)
        except Exception:
            _orch_subscribers.get(goal_id, set()).discard(send_fn)


# ── Orchestrator channel page (read-only for humans) ─────────────────────────

@router.get("/goals/{pid}/orchestrator", response_class=HTMLResponse)
async def orchestrator_page(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1 AND status = 'active'", pid)
    if not goal:
        raise HTTPException(status_code=404)
    goal = dict(goal)

    orch_thread_id = goal.get("orchestrator_thread_id", "")
    if not orch_thread_id:
        raise HTTPException(status_code=404, detail="Orchestrator channel not found")

    # Only the newest window is rendered; older history is paged in on demand.
    # See app/routers/thread_paging.py for why this is keyset-based.
    messages, has_older = await fetch_channel_page(pool, orch_thread_id)
    _log.info(
        "orchestrator-channel: rendering goal=%s messages=%d has_older=%s",
        pid, len(messages), has_older,
    )

    firebase_token = request.cookies.get("firebase_token", "")

    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "agent-mesh-intro")

    return templates.TemplateResponse(
        "orchestrator/index.html",
        {
            "request": request, "user": user, "goal": goal, "pid": pid,
            "thread_id": orch_thread_id, "messages": messages,
            "firebase_token": firebase_token,
            "auto_fire_tour": auto_fire_tour,
            "has_older": has_older,
            "oldest_message_id": messages[0]["id"] if messages else "",
            "accent": _ACCENT,
            # This channel carries both sides of an internal exchange, so rows
            # are labelled by role (GO / System / Assistant), not by sender id.
            "label_mode": "role",
            "load_earlier_url": f"/goals/{pid}/orchestrator/messages/earlier",
        },
    )


register_channel_earlier_route(
    router,
    channel_slug="orchestrator",
    thread_id_column="orchestrator_thread_id",
    accent=_ACCENT,
    label_mode="role",
)


# ── WebSocket (read-only broadcast to human observers) ────────────────────────

@router.websocket("/goals/{pid}/orchestrator/ws")
async def orchestrator_ws(pid: str, websocket: WebSocket, token: str):
    """WebSocket for observing the orchestrator channel. Humans can watch but not send."""
    try:
        claims = verify_firebase_token(token)
        uid = claims["uid"]
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    role = await get_goal_role(uid, pid)
    if role is None:
        await websocket.close(code=4003, reason="Not a goal member")
        return
    user = {"uid": uid}

    await websocket.accept()

    unsubscribe = _subscribe_to_orchestrator(pid, websocket.send_text)
    _log.info("Orchestrator subscriber added: goal=%s, user=%s", pid, user["uid"])

    try:
        while True:
            # Human can't send messages — just keep connection alive
            data = await websocket.receive_text()
            # Ignore any messages from humans (read-only channel)
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
        _log.info("Orchestrator subscriber removed: goal=%s, user=%s", pid, user["uid"])


# ── Internal API: SHCA → GO (assistant posts to orchestrator) ─────────────────

@router.post("/internal/goals/{pid}/orchestrator/message")
async def internal_orchestrator_message(pid: str, request: Request):
    """SHCA posts a message to the orchestrator channel. Used by message_go."""
    verify_internal_bearer(request)

    body = await request.json()
    message = body.get("message", "").strip()
    msg_type = body.get("type", "update")  # question | update | request
    story_id = body.get("story_id", "")
    gateway_agent_id = body.get("gateway_agent_id", "")

    if not message:
        return JSONResponse({"ok": False, "error": "message required"}, status_code=400)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT orchestrator_thread_id, go_agent_id, go_thread_id FROM goals WHERE id = $1", pid,
    )
    if not goal or not goal["orchestrator_thread_id"]:
        return JSONResponse({"ok": False, "error": "Orchestrator channel not found"}, status_code=404)

    orch_thread_id = goal["orchestrator_thread_id"]

    # Resolve story context and agent name
    story_title = ""
    agent_name = "Assistant"
    if story_id:
        story = await pool.fetchrow(
            "SELECT title, assigned_to FROM stories WHERE id = $1", story_id,
        )
        if story:
            story_title = story["title"]
            # Use the assigned human's name for attribution
            if story.get("assigned_to"):
                user_name = await pool.fetchval(
                    "SELECT display_name FROM users WHERE uid = $1", story["assigned_to"],
                )
                if user_name:
                    agent_name = f"{user_name}'s Assistant"

    # Fallback: resolve from gateway_agent_id
    if agent_name == "Assistant" and gateway_agent_id:
        agent_row = await pool.fetchrow(
            "SELECT name FROM agents WHERE gateway_agent_id = $1 OR id = $1", gateway_agent_id,
        )
        if agent_row:
            agent_name = agent_row["name"]

    # Format attribution
    story_attr = f" · Story: {story_title}" if story_title else ""
    type_attr = f" · {msg_type}" if msg_type != "update" else ""
    formatted = f"[{agent_name}{story_attr}{type_attr}]\n{message}"

    # Insert into orchestrator thread
    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
           VALUES ($1, $2, $3, 'human', $4, 0)""",
        msg_id, orch_thread_id, gateway_agent_id or "assistant", formatted,
    )

    # Broadcast to UI observers
    await _broadcast_to_orchestrator(pid, json.dumps({
        "type": "message",
        "message": {
            "id": msg_id, "sender": gateway_agent_id or "assistant",
            "role": "human", "content": formatted,
        },
    }))

    # Mirror to Slack orch channel (if Slack enabled for this goal)
    try:
        from app.channels.slack.outbound import post_to_orchestrator_channel
        await post_to_orchestrator_channel(pid, formatted, username=agent_name)
    except ImportError:
        pass
    except Exception as exc:
        _log.warning("Failed to relay orch message to Slack: %s", exc)

    try:
        from app.channels.notion.outbound import relay_orchestrator_message
        await relay_orchestrator_message(pid, formatted)
    except ImportError:
        pass
    except Exception as exc:
        _log.warning("Notion orch relay failed: %s", exc)

    # If type="update", also merge into story's progress_summary
    if msg_type == "update" and story_id:
        try:
            from app.routers.stories import _merge_progress_summary
            story_row = await pool.fetchrow(
                "SELECT progress_summary FROM stories WHERE id = $1", story_id,
            )
            if story_row:
                current = story_row["progress_summary"] or ""
                merged = await _merge_progress_summary(current, message)
                await pool.execute(
                    "UPDATE stories SET progress_summary = $1, updated_at = now() WHERE id = $2",
                    merged, story_id,
                )
        except Exception as exc:
            _log.warning("Failed to merge progress summary for story %s: %s", story_id, exc)

    # Send to GO's session — auto-create if not active
    try:
        go_agent_id = goal["go_agent_id"]
        go_thread_id = goal["go_thread_id"]
        session_key = (pid, go_agent_id)
        session = session_manager._sessions.get(session_key)

        # Deleted goal → don't auto-create a GO session for it.
        from app.services.goal_guard import goal_is_dead
        if not session and go_agent_id and not await goal_is_dead(pid, pool):
            # Auto-create GO session so it can process the message
            from app.routers.goal_thread import _get_go_context, _make_go_callbacks
            go_context = await _get_go_context(pool, pid, go_agent_id)
            on_message, on_notify = _make_go_callbacks(pool, pid, go_agent_id, go_thread_id)
            session = session_manager.get_or_create(
                pid, go_agent_id, go_context, on_message, on_notify,
                thread_id=go_thread_id,
            )
            gw_id = go_context.get("gateway_agent_id", "")
            if gw_id:
                session._gateway_agent_id = gw_id
            _log.info("Auto-created GO session for orchestrator message: goal=%s", pid)

        if session:
            # Slim context (goal identity + team roster only)
            context = await build_go_context(pid, go_thread_id)
            go_msg_content = (
                f"{context}\n\n---\n\n"
                f"[Orchestrator Channel Message]\n{formatted}\n\n"
                f"---\n\n"
                f"INSTRUCTION: You MUST respond to this assistant message. Call the "
                f"`reply_assistant` tool with story_id=\"{story_id}\" and your message. "
                f"This is not optional — every orchestrator message requires a reply, even "
                f"if it's just an acknowledgement like \"Thanks for the update, keep going.\" "
                f"After calling the tool, you may send a brief confirmation message.\n"
                f"Do NOT respond via plain text in this conversation — use the tool."
            )
            # send_to_gateway expects JSON with type=message, content, thread_id.
            # queue_kind="system_wake" is required — the default ("user") persists
            # this as a normal kind='user' row, which slips past the goal-thread
            # render filter (excludes kind='system_wake', not sender='system') and
            # renders the GO-internal reply-instruction wrapper as a visible bubble.
            # The human-readable assistant message itself is already mirrored to
            # Agent Mesh above (lines ~202-234) — this copy is GO-only context.
            await session.send_to_gateway(json.dumps({
                "type": "message",
                "content": go_msg_content,
                "thread_id": go_thread_id,
            }), queue_kind="system_wake")
            _log.info("Forwarded orchestrator message to GO: goal=%s, story=%s", pid, story_id)
        else:
            _log.warning("No GO agent configured for goal %s", pid)
    except Exception as exc:
        _log.warning("Failed to forward to GO: %s", exc)

    return JSONResponse({"ok": True, "message_id": msg_id})


# ── Internal API: GO → SHCA (GO replies to a specific assistant) ──────────────

@router.post("/internal/goals/{pid}/orchestrator/reply")
async def internal_orchestrator_reply(pid: str, request: Request):
    """GO replies to a specific story's assistant. Used by reply_assistant."""
    verify_internal_bearer(request)

    body = await request.json()
    story_id = body.get("story_id", "").strip()
    message = body.get("message", "").strip()

    if not story_id or not message:
        return JSONResponse({"ok": False, "error": "story_id and message required"}, status_code=400)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT orchestrator_thread_id FROM goals WHERE id = $1", pid,
    )
    if not goal or not goal["orchestrator_thread_id"]:
        return JSONResponse({"ok": False, "error": "Orchestrator channel not found"}, status_code=404)

    story = await pool.fetchrow(
        "SELECT title, active_thread_id, assigned_to FROM stories WHERE id = $1 AND goal_id = $2",
        story_id, pid,
    )
    if not story:
        return JSONResponse({"ok": False, "error": f"Story {story_id} not found"}, status_code=404)

    story_title = story["title"]
    formatted = f"[GO → Story: {story_title}]\n{message}"

    # 1. Insert into orchestrator thread
    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
           VALUES ($1, $2, 'go', 'agent', $3, 0)""",
        msg_id, goal["orchestrator_thread_id"], formatted,
    )

    # Broadcast to UI observers
    await _broadcast_to_orchestrator(pid, json.dumps({
        "type": "message",
        "message": {
            "id": msg_id, "sender": "go", "role": "agent", "content": formatted,
        },
    }))

    # Mirror to Slack orch channel
    try:
        from app.channels.slack.outbound import post_to_orchestrator_channel
        await post_to_orchestrator_channel(pid, formatted, username="Goal Orchestrator")
    except ImportError:
        pass
    except Exception as exc:
        _log.warning("Failed to relay GO reply to Slack orch channel: %s", exc)

    try:
        from app.channels.notion.outbound import relay_orchestrator_message
        await relay_orchestrator_message(pid, formatted)
    except ImportError:
        pass
    except Exception as exc:
        _log.warning("Notion orch relay failed: %s", exc)

    # 2. Deliver to the SHCA's conversation thread (via active_thread_id)
    shca_thread_id = story.get("active_thread_id", "")
    _log.info(
        "reply_assistant: story=%s, assigned_to=%s, active_thread_id=%s",
        story_id, story.get("assigned_to", "?"), shca_thread_id or "(empty)",
    )
    delivered_to_shca = False
    delivered_to_session = False
    if shca_thread_id:
        shca_msg_id = str(uuid.uuid4())
        shca_content = f"[Goal Orchestrator]\n{message}"
        await pool.execute(
            """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
               VALUES ($1, $2, 'go', 'agent', $3, 0)""",
            shca_msg_id, shca_thread_id, shca_content,
        )
        delivered_to_shca = True

        # Relay the GO's reply to all connected thread channels (Slack, Notion SHCA page, etc.)
        try:
            from app.channels.base import relay_to_thread_channel
            await relay_to_thread_channel(pid, shca_thread_id, message, username="Goal Orchestrator")
        except Exception as exc:
            _log.warning("Channel thread relay failed for GO reply: %s", exc)

        # Try to send to the SHCA's active gateway session
        try:
            assigned_to = story.get("assigned_to", "")
            if assigned_to:
                # Find the assistant agent for this user
                from app.services.agent_registry.assistant_queries import get_user_assistant_agent_id_by_uid
                agent_id = await get_user_assistant_agent_id_by_uid(pool, assigned_to)
                if agent_id:
                    session_key = (pid, agent_id, shca_thread_id)
                    session = session_manager._sessions.get(session_key)
                    if not session:
                        # Auto-start so the GO's reply actually triggers the SHCA.
                        from app.services.agent_driver import ensure_shca_session
                        session = await ensure_shca_session(pid, agent_id, shca_thread_id)
                    if session:
                        # send_to_gateway expects a JSON envelope
                        # ({type:"message", content, thread_id}); passing raw
                        # text causes a silent return and the SHCA never sees
                        # the reply — this is the Slack "no response after GO
                        # replies" bug.
                        await session.send_to_gateway(json.dumps({
                            "type": "message",
                            "content": shca_content,
                            "thread_id": shca_thread_id,
                        }))
                        delivered_to_session = True
                        _log.info("Sent GO reply to SHCA gateway session for story %s", story_id)
                    else:
                        _log.info("SHCA session not active for story %s — reply stored in DB", story_id)
        except Exception as exc:
            _log.warning("Failed to send to SHCA session: %s", exc)

        _log.info("Delivered GO reply to SHCA thread %s for story %s", shca_thread_id[:12], story_id)
    else:
        _log.warning("No active_thread_id for story %s — GO reply not delivered to SHCA", story_id)

    return JSONResponse({
        "ok": True, "message_id": msg_id,
        "delivered_to_shca": delivered_to_shca,
        "shca_thread_id": shca_thread_id,
    })


# ── Internal API: Decision log ────────────────────────────────────────────────

@router.post("/internal/goals/{pid}/decisions")
async def internal_record_decision(pid: str, request: Request):
    """Record a goal decision. Used by record_decision."""
    verify_internal_bearer(request)

    body = await request.json()
    decision = body.get("decision", "").strip()
    rationale = body.get("rationale", "")
    story_id = body.get("story_id", "")
    decided_by = body.get("decided_by", "")

    if not decision:
        return JSONResponse({"ok": False, "error": "decision required"}, status_code=400)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    decision_id = str(uuid.uuid4())[:12]
    await pool.execute("""
        INSERT INTO goal_decisions (id, goal_id, decision, rationale, decided_by, story_id)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, decision_id, pid, decision, rationale, decided_by, story_id)

    # Mirror decision to Slack orch channel (goal channel covered by relay_to_goal_channel below)
    try:
        from app.channels.slack.outbound import post_to_orchestrator_channel as _slack_orch
        _story_suffix = ""
        if story_id:
            _story_title = await pool.fetchval("SELECT title FROM stories WHERE id = $1", story_id)
            if _story_title:
                _story_suffix = f" · story: *{_story_title}*"
        _orch_msg = f"📝 *Decision recorded:* {decision}{_story_suffix}"
        if rationale:
            _orch_msg += f"\n_Rationale:_ {rationale[:800]}"
        await _slack_orch(pid, _orch_msg, username="Goal Orchestrator")
    except ImportError:
        pass
    except Exception as exc:
        _log.warning("Slack decision-recorded orch relay failed: %s", exc)

    # Relay to all other connected goal channels (Notion goal page, etc.)
    try:
        from app.channels.base import relay_to_goal_channel
        _msg_parts = [f"📝 *Decision recorded:* {decision}"]
        if story_id:
            _story_title = await pool.fetchval("SELECT title FROM stories WHERE id = $1", story_id)
            if _story_title:
                _msg_parts[0] += f" · story: *{_story_title}*"
        if rationale:
            _msg_parts.append(f"Rationale: {rationale[:800]}")
        _decision_msg = "\n".join(_msg_parts)
        await relay_to_goal_channel(pid, _decision_msg, username="Goal Orchestrator")
        from app.channels.notion.outbound import relay_orchestrator_message
        await relay_orchestrator_message(pid, _decision_msg)
    except Exception as exc:
        _log.warning("Channel goal relay failed for decision-recorded: %s", exc)

    _log.info("Decision recorded for goal %s: %s", pid, decision[:60])
    return JSONResponse({"ok": True, "decision_id": decision_id})


@router.get("/internal/goals/{pid}/decisions")
async def internal_list_decisions(pid: str, request: Request):
    """List all decisions for a goal. Used by list_decisions."""
    verify_internal_bearer(request)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT id, decision, rationale, decided_by, story_id, created_at
        FROM goal_decisions WHERE goal_id = $1
        ORDER BY created_at
    """, pid)
    decisions = []
    for r in rows:
        d = dict(r)
        d["created_at"] = str(d["created_at"]) if d.get("created_at") else ""
        decisions.append(d)

    return JSONResponse({"decisions": decisions, "goal_id": pid})


# ── Internal API: Resource allocations (unified grant / revoke / list) ───────
#
# One surface for both GO (story-level) and SHCAs (task-level). Backed by:
#   - story_resources   (subject_type='story') — from migration 019.
#   - task_resource_deps (subject_type='task') — from migration 014;
#     columns (task_id, resource_id→goal_resources.id, dep_type, notes,
#     created_by, created_at). PRIMARY KEY (task_id, resource_id, dep_type)
#     permits multiple rows per (task, resource); we enforce single-row
#     semantics in app logic via DELETE-then-INSERT on grant/revoke so
#     the RESOURCES.md renderer sees exactly one effective relation.
#
# Task allocations are ADDITIVE on top of the story's inherited set, except
# when dep_type='excluded' which SUBTRACTS a specific resource from a single
# task's view (the story still has it).
#
# The legacy `task_resources` table (migration 008, FK to resources(id),
# access_level/paths) is untouched and remains in use by
# _auto_populate_task_resources in app/routers/tasks.py — orthogonal to
# allocations.


# Allocation grant/revoke logic + the post-change workspace refresh now live in
# app/services/resource_allocation.py (shared with the Slack /resources command).
# These endpoints delegate to that service — see internal_grant_resource below.


@router.post("/internal/goals/{pid}/allocations/grant")
async def internal_grant_resource(pid: str, request: Request):
    """Grant a resource to a story or task.

    Body:
      {
        "subject_type": "story" | "task",
        "subject_id":   "...",
        "resource_id":  "...",
        "notes":        "GO's or SHCA's tactical guidance (rendered inline)",
        "relation":     "needs" | "produces" | "modifies",   # default: needs
        "actor_id":     "go-<pid>" | "<agent_id>"            # optional; for audit
      }

    For subject_type='task': if an 'excluded' row already exists for this
    (task, resource), it's replaced with the new relation — i.e. the
    resource becomes visible to the task again.
    """
    verify_internal_bearer(request)

    body = await request.json()
    subject_type = (body.get("subject_type") or "").strip()
    subject_id = (body.get("subject_id") or "").strip()
    resource_id = (body.get("resource_id") or "").strip()
    relation = (body.get("relation") or "needs").strip()
    notes = body.get("notes") or ""
    actor_id = (body.get("actor_id") or "").strip()

    # Business logic lives in the shared service (app/services/resource_allocation.py)
    # so the Slack /resources grant command and these agent-tool endpoints behave
    # identically. The service enforces the Git Engine grant rules (reject the trunk
    # branch, allow a repo as a story's target, one-repo-per-story) and returns the
    # agent-facing rejection messages.
    from app.services.resource_allocation import grant_resource
    ok, message = await grant_resource(
        pid, subject_type, subject_id, resource_id, relation, notes, actor_id,
    )
    if not ok:
        status = 404 if "not found" in message else 400
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    return JSONResponse({
        "ok": True,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "resource_id": resource_id,
        "relation": relation,
    })


@router.post("/internal/goals/{pid}/allocations/revoke")
async def internal_revoke_resource(pid: str, request: Request):
    """Revoke a resource from a story or task.

    Body:
      {
        "subject_type": "story" | "task",
        "subject_id":   "...",
        "resource_id":  "...",
        "actor_id":     "<agent_id>"   # optional; for audit
      }

    For subject_type='story': deletes the story_resources row. The
    resource is gone from every SHCA session for this story AND from
    every task in this story (unless a task has it task-level-granted).

    For subject_type='task':
      - If a row exists with a non-excluded relation → delete it (the
        addition is rolled back).
      - Otherwise (the resource is inherited from the story), insert a
        row with relation='excluded' so it's filtered out of THIS task's
        RESOURCES.md only. The story still has the resource.
    """
    verify_internal_bearer(request)

    body = await request.json()
    subject_type = (body.get("subject_type") or "").strip()
    subject_id = (body.get("subject_id") or "").strip()
    resource_id = (body.get("resource_id") or "").strip()
    actor_id = (body.get("actor_id") or "").strip()

    # Shared service — see internal_grant_resource for the rationale.
    from app.services.resource_allocation import revoke_resource
    ok, message = await revoke_resource(
        pid, subject_type, subject_id, resource_id, actor_id,
    )
    if not ok:
        status = 404 if "not found" in message else 400
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    return JSONResponse({
        "ok": True,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "resource_id": resource_id,
    })


@router.get("/internal/goals/{pid}/allocations")
async def internal_list_allocations(pid: str, request: Request):
    """List current allocations for a subject.

    Query params:
      subject_type = story | task | goal
      subject_id   = required for story/task (ignored for goal)

    Response shape:
      {
        "goal_id": "...",
        "subject_type": "...",
        "subject_id": "...",
        "allocations": [
          {"resource_id": "...", "name": "...", "kind": "...",
           "relation": "needs|produces|modifies|excluded",
           "notes": "...", "source": "story" | "task" | "inherited"},
          ...
        ]
      }
    """
    verify_internal_bearer(request)

    subject_type = (request.query_params.get("subject_type") or "").strip()
    subject_id = (request.query_params.get("subject_id") or "").strip()

    if subject_type not in ("story", "task", "goal"):
        return JSONResponse(
            {"ok": False, "error": "subject_type must be story|task|goal"},
            status_code=400,
        )
    if subject_type != "goal" and not subject_id:
        return JSONResponse({"ok": False, "error": "subject_id required"}, status_code=400)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    allocations: list[dict] = []

    if subject_type == "goal":
        rows = await pool.fetch(
            """SELECT id AS resource_id, name, kind
                 FROM goal_resources
                WHERE goal_id = $1 AND status = 'active'
                ORDER BY kind, name""",
            pid,
        )
        for r in rows:
            allocations.append({
                "resource_id": r["resource_id"], "name": r["name"], "kind": r["kind"],
                "relation": "n/a", "notes": "", "source": "goal",
            })
    elif subject_type == "story":
        rows = await pool.fetch(
            """SELECT gr.id AS resource_id, gr.name, gr.kind,
                      sr.relation, sr.notes
                 FROM story_resources sr
                 JOIN goal_resources gr ON gr.id = sr.resource_id
                WHERE sr.story_id = $1 AND gr.status = 'active'
                ORDER BY gr.kind, gr.name""",
            subject_id,
        )
        for r in rows:
            allocations.append({
                "resource_id": r["resource_id"], "name": r["name"], "kind": r["kind"],
                "relation": r["relation"], "notes": r["notes"] or "", "source": "story",
            })
    else:
        # subject_type == "task" — union of (inherited story + task-level),
        # minus any exclusions.
        story_id = await pool.fetchval(
            "SELECT story_id FROM tasks WHERE id = $1", subject_id,
        )
        # task_resource_deps PK (task_id, resource_id, dep_type) allows
        # multiple rows per (task, resource); our app logic writes only one,
        # but defensively take the latest row per pair.
        task_rows = await pool.fetch(
            """SELECT DISTINCT ON (resource_id)
                      resource_id, dep_type AS relation, notes
                 FROM task_resource_deps
                WHERE task_id = $1
                ORDER BY resource_id, created_at DESC""",
            subject_id,
        )
        excluded_ids = {r["resource_id"] for r in task_rows if r["relation"] == "excluded"}
        task_added = {r["resource_id"]: r for r in task_rows if r["relation"] != "excluded"}

        if story_id:
            story_rows = await pool.fetch(
                """SELECT gr.id AS resource_id, gr.name, gr.kind,
                          sr.relation, sr.notes
                     FROM story_resources sr
                     JOIN goal_resources gr ON gr.id = sr.resource_id
                    WHERE sr.story_id = $1 AND gr.status = 'active'""",
                story_id,
            )
            for r in story_rows:
                if r["resource_id"] in excluded_ids:
                    continue
                allocations.append({
                    "resource_id": r["resource_id"], "name": r["name"], "kind": r["kind"],
                    "relation": r["relation"],
                    "notes": r["notes"] or "",
                    "source": "inherited",
                })
        if task_added:
            info_rows = await pool.fetch(
                """SELECT id, name, kind FROM goal_resources
                    WHERE id = ANY($1) AND status = 'active'""",
                list(task_added.keys()),
            )
            for r in info_rows:
                ta = task_added[r["id"]]
                allocations.append({
                    "resource_id": r["id"], "name": r["name"], "kind": r["kind"],
                    "relation": ta["relation"], "notes": ta["notes"] or "",
                    "source": "task",
                })

    return JSONResponse({
        "ok": True,
        "goal_id": pid,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "allocations": allocations,
    })


# ── Internal API: Resource registration by assistants ─────────────────────────

def _parse_github_url(uri: str) -> Optional[dict]:
    """Parse a GitHub URL into owner/repo/branch components.

    Handles:
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      https://github.com/owner/repo/tree/branch-name
      git@github.com:owner/repo.git
    Returns dict with 'owner', 'repo', and optionally 'branch', or None if not a GitHub URL.
    """
    import re
    if not uri:
        return None
    uri = uri.strip().rstrip("/")

    # SSH form: git@github.com:owner/repo.git
    ssh_match = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", uri)
    if ssh_match:
        return {"owner": ssh_match.group(1), "repo": ssh_match.group(2)}

    # HTTPS form
    https_match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/(.+))?$", uri
    )
    if https_match:
        result: dict = {"owner": https_match.group(1), "repo": https_match.group(2)}
        if https_match.group(3):
            result["branch"] = https_match.group(3)
        return result

    return None


@router.post("/internal/goals/{pid}/resources/register")
async def internal_register_resource(pid: str, request: Request):
    """Register a new resource created by an assistant. Used by register_resource."""
    verify_internal_bearer(request)

    body = await request.json()
    name = body.get("name", "").strip()
    kind = body.get("kind", "other")
    uri = (body.get("uri") or body.get("clone_url") or "").strip()
    story_id = body.get("story_id", "")
    description = body.get("description", "")

    # Git-specific fields — accept explicit values or parse from URI
    branch_name = (body.get("branch_name") or body.get("branch") or "").strip()
    repo_name = (body.get("repo_name") or body.get("repo") or "").strip()
    repo_owner = (body.get("repo_owner") or body.get("owner") or "").strip()
    clone_url = (body.get("clone_url") or "").strip()
    parent_id = (body.get("parent_id") or "").strip() or None
    # New (Phase 2) — agent-registerable metadata for kinds that carry
    # structured config (gcp_environment, gcp_test_sa, gcp_deploy_sa, etc.).
    # Legacy kinds (repo, branch, document, ...) ignore this — their
    # config lives in top-level columns.
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else None

    if kind in ("repo", "branch") and uri:
        parsed = _parse_github_url(uri)
        if parsed:
            repo_owner = repo_owner or parsed["owner"]
            repo_name = repo_name or parsed["repo"]
            branch_name = branch_name or parsed.get("branch", "")
    if not clone_url and repo_owner and repo_name:
        clone_url = f"https://github.com/{repo_owner}/{repo_name}.git"

    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)

    pid = await resolve_goal_id(pid)
    pool = get_pool()

    # Resource-kind registry validation — see app/resource_kinds.py.
    # For kinds with a `human_only` flag (e.g. repo), the registry's
    # validate_registration() returns the operator-friendly rejection
    # message; we keep the existing repo-specific message below as a
    # special case because it's longer + more guidance-y than the
    # generic registry message.
    if kind == "repo":
        _log.warning(
            "internal_register_resource: rejected kind='repo' (goal=%s story=%s) — "
            "repos must be added by humans via the Resources panel",
            pid, story_id or "-",
        )
        return JSONResponse(
            {"ok": False, "error": (
                "kind='repo' is not allowed via register_resource. "
                "Adding a repository requires two steps that must be done by a human: "
                "(1) install the GitHub App on the target repository on GitHub, "
                "(2) then add the repository to this goal via the goal's Resources panel — "
                "it will appear there automatically once the app is installed. "
                "Tell the human these two steps and wait. "
                "Once a repo resource exists, use create_branch to create a branch from it."
            )},
            status_code=400,
        )

    # Fetch parent's row (kind + metadata) so the registry can run
    # cross-resource constraints (e.g. SA project must match parent env's
    # project). Done before validation so we can fail fast.
    parent_metadata: dict | None = None
    if parent_id:
        parent_row = await pool.fetchrow(
            "SELECT kind, metadata FROM goal_resources "
            "WHERE id = $1 AND goal_id = $2",
            parent_id, pid,
        )
        if not parent_row:
            return JSONResponse(
                {"ok": False, "error": (
                    f"parent_id {parent_id!r} does not refer to a resource in "
                    f"this goal. Pass the id of an existing resource."
                )},
                status_code=400,
            )
        raw_pm = parent_row["metadata"]
        if isinstance(raw_pm, str):
            try:
                parent_metadata = json.loads(raw_pm)
            except (ValueError, TypeError):
                parent_metadata = None
        elif isinstance(raw_pm, dict):
            parent_metadata = raw_pm

    # Generic registry-based validation. Returns None on success, or an
    # operator-readable error string. Legacy kinds whose metadata_fields
    # tuple is empty pass through unchanged.
    from app.resource_kinds import validate_registration, get_spec  # noqa: PLC0415
    spec = get_spec(kind)
    if spec is None:
        return JSONResponse(
            {"ok": False, "error": (
                f"Unknown resource kind {kind!r}. Call search_resource_kinds "
                f"to discover supported kinds, or point the human at the goal's "
                f"Resources panel."
            )},
            status_code=400,
        )
    error = validate_registration(
        kind=kind, metadata=metadata or {}, parent_metadata=parent_metadata,
    )
    if error:
        _log.info(
            "internal_register_resource: validation rejected (goal=%s kind=%s): %s",
            pid, kind, error,
        )
        return JSONResponse({"ok": False, "error": error}, status_code=400)

    resource_id = str(uuid.uuid4())[:12]

    await pool.execute("""
        INSERT INTO goal_resources (id, goal_id, kind, name, uri, created_by, human_context,
                                     source_task_id, summary_stale,
                                     branch_name, repo_name, repo_owner, clone_url, parent_id,
                                     metadata)
        VALUES ($1, $2, $3, $4, $5, 'assistant', $6, $7, true, $8, $9, $10, $11, $12, $13)
    """, resource_id, pid, kind, name, uri, description, story_id,
         branch_name or None, repo_name or None, repo_owner or None,
         clone_url or None, parent_id,
         json.dumps(metadata) if metadata else None)

    # Link as "produces" on the story
    if story_id:
        await pool.execute("""
            INSERT INTO story_resources (story_id, resource_id, relation)
            VALUES ($1, $2, 'produces')
            ON CONFLICT DO NOTHING
        """, story_id, resource_id)

    # Notify GO via orchestrator channel
    goal = await pool.fetchrow(
        "SELECT orchestrator_thread_id FROM goals WHERE id = $1", pid,
    )
    if goal and goal["orchestrator_thread_id"]:
        notify_msg = f"[System] New resource registered: {name} ({kind})"
        if story_id:
            story_title = await pool.fetchval("SELECT title FROM stories WHERE id = $1", story_id)
            if story_title:
                notify_msg += f" — produced by story \"{story_title}\""
        msg_id = str(uuid.uuid4())
        await pool.execute(
            """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
               VALUES ($1, $2, 'system', 'human', $3, 0)""",
            msg_id, goal["orchestrator_thread_id"], notify_msg,
        )
        await _broadcast_to_orchestrator(pid, json.dumps({
            "type": "message",
            "message": {"id": msg_id, "sender": "system", "role": "human", "content": notify_msg},
        }))

        # Mirror to Slack orch channel
        try:
            from app.channels.slack.outbound import post_to_orchestrator_channel
            await post_to_orchestrator_channel(pid, notify_msg, username="System")
        except ImportError:
            pass
        except Exception as exc:
            _log.warning("Slack resource-registered relay failed: %s", exc)

    _log.info(
        "Resource registered goal=%s rid=%s name=%s kind=%s story=%s",
        pid, resource_id, name, kind, story_id or "-",
    )

    # If linked to a story, refresh that story's SHCA + any IA workers
    if story_id:
        try:
            from app.resource_provisioning import (
                refresh_story_resources, refresh_task_workers_for_story,
            )
            shca_ok = await refresh_story_resources(pid, story_id)
            ia_count = await refresh_task_workers_for_story(pid, story_id)
            _log.info(
                "post-register_resource refresh: goal=%s story=%s rid=%s shca_ok=%s ia_workers=%d",
                pid, story_id, resource_id, shca_ok, ia_count,
            )
        except Exception as exc:
            _log.warning("refresh after register_resource failed for %s: %s", story_id, exc)

    return JSONResponse({"ok": True, "resource_id": resource_id, "name": name})


# ── Internal API: Branch creation ─────────────────────────────────────────────

@router.post("/internal/goals/{pid}/resources/{rid}/branch")
async def internal_create_branch(pid: str, rid: str, request: Request):
    """Create a feature branch from a repo/branch resource. Used by create_branch."""
    verify_internal_bearer(request)

    body = await request.json()
    branch_name = body.get("branch_name", "").strip()
    story_id = body.get("story_id", "")

    if not branch_name:
        return JSONResponse({"ok": False, "error": "branch_name required"}, status_code=400)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    resource = await pool.fetchrow(
        "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2", rid, pid,
    )
    if not resource:
        return JSONResponse({"ok": False, "error": "Resource not found"}, status_code=404)

    repo_owner = resource.get("repo_owner", "")
    repo_name = resource.get("repo_name", "")
    source_branch = resource.get("branch_name", "") or resource.get("default_branch", "main")

    if not repo_owner or not repo_name:
        return JSONResponse({"ok": False, "error": "Resource is not a repo/branch"}, status_code=400)

    # Get GitHub token
    try:
        from app.routers.go_internal import _get_github_token
        token = await _get_github_token(pid, repo_name)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"GitHub token error: {exc}"}, status_code=500)

    # Get source branch SHA
    import httpx
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    async with httpx.AsyncClient() as client:
        # Get SHA of source branch
        ref_resp = await client.get(
            f"https://api.github.com/repos/{repo_owner}/{repo_name}/git/ref/heads/{source_branch}",
            headers=headers,
        )
        if ref_resp.status_code != 200:
            return JSONResponse({
                "ok": False, "error": f"Source branch '{source_branch}' not found: {ref_resp.status_code}",
            }, status_code=400)

        sha = ref_resp.json()["object"]["sha"]

        # Create new branch
        create_resp = await client.post(
            f"https://api.github.com/repos/{repo_owner}/{repo_name}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch_name}", "sha": sha},
        )
        if create_resp.status_code not in (200, 201):
            error_msg = create_resp.json().get("message", "Unknown error")
            return JSONResponse({"ok": False, "error": f"Branch creation failed: {error_msg}"}, status_code=400)

    # Register as new goal resource
    new_rid = str(uuid.uuid4())[:12]
    await pool.execute("""
        INSERT INTO goal_resources (id, goal_id, kind, name, uri, created_by,
                                     repo_owner, repo_name, branch_name, base_branch,
                                     clone_url, parent_id, summary_stale)
        VALUES ($1, $2, 'branch', $3, $4, $5, $6, $7, $8, $9, $10, $11, true)
    """, new_rid, pid, f"{repo_name}:{branch_name}",
        f"https://github.com/{repo_owner}/{repo_name}/tree/{branch_name}",
        f"go-{pid}", repo_owner, repo_name, branch_name, source_branch,
        f"https://github.com/{repo_owner}/{repo_name}", rid)

    # Link to story as "produces"
    if story_id:
        await pool.execute("""
            INSERT INTO story_resources (story_id, resource_id, relation)
            VALUES ($1, $2, 'produces') ON CONFLICT DO NOTHING
        """, story_id, new_rid)

    _log.info(
        "Created branch goal=%s repo=%s/%s branch=%s from=%s new_rid=%s story=%s",
        pid, repo_owner, repo_name, branch_name, source_branch, new_rid, story_id or "-",
    )

    # If linked to a story, refresh that story's SHCA + any IA workers
    if story_id:
        try:
            from app.resource_provisioning import (
                refresh_story_resources, refresh_task_workers_for_story,
            )
            shca_ok = await refresh_story_resources(pid, story_id)
            ia_count = await refresh_task_workers_for_story(pid, story_id)
            _log.info(
                "post-create_branch refresh: goal=%s story=%s branch=%s shca_ok=%s ia_workers=%d",
                pid, story_id, branch_name, shca_ok, ia_count,
            )
        except Exception as exc:
            _log.warning("refresh after create_branch failed for %s: %s", story_id, exc)

    return JSONResponse({
        "ok": True, "resource_id": new_rid, "branch_name": branch_name,
        "repo": f"{repo_owner}/{repo_name}", "source_branch": source_branch,
    })


# ── Internal API: Critical path ───────────────────────────────────────────────

@router.get("/internal/goals/{pid}/critical-path")
async def internal_critical_path(pid: str, request: Request):
    """Compute the critical path (longest dependency chain). Used by critical_path."""
    verify_internal_bearer(request)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    stories = await pool.fetch("""
        SELECT id, title, state, depends_on, estimated_effort
        FROM stories WHERE goal_id = $1 AND state NOT IN ('done', 'cancelled')
    """, pid)

    if not stories:
        return JSONResponse({"critical_path": [], "total_effort": "0", "parallel_stories": []})

    stories = [dict(s) for s in stories]
    graph = {s["id"]: s for s in stories}

    # Parse effort estimates to days (rough)
    def effort_days(effort_str):
        if not effort_str:
            return 1
        effort_str = effort_str.lower()
        for word in effort_str.split():
            try:
                num = float(word)
                if "week" in effort_str:
                    return num * 5
                return num
            except ValueError:
                continue
        return 1

    # Longest path via DFS
    memo = {}
    def longest_path(sid):
        if sid in memo:
            return memo[sid]
        s = graph.get(sid)
        if not s:
            return (0, [])
        deps = s.get("depends_on") or []
        best_len = 0
        best_path = []
        for dep_id in deps:
            if dep_id in graph:
                dep_len, dep_path = longest_path(dep_id)
                if dep_len > best_len:
                    best_len = dep_len
                    best_path = dep_path
        result = (best_len + effort_days(s.get("estimated_effort", "")), best_path + [sid])
        memo[sid] = result
        return result

    all_paths = [longest_path(s["id"]) for s in stories]
    if not all_paths:
        return JSONResponse({"critical_path": [], "total_effort": "0", "parallel_stories": []})

    critical = max(all_paths, key=lambda x: x[0])
    critical_ids = set(critical[1])
    parallel = [s["id"] for s in stories if s["id"] not in critical_ids]

    return JSONResponse({
        "critical_path": [
            {"id": sid, "title": graph[sid]["title"], "effort": graph[sid].get("estimated_effort", "")}
            for sid in critical[1] if sid in graph
        ],
        "total_effort": f"{critical[0]} days",
        "parallel_stories": [
            {"id": sid, "title": graph[sid]["title"]}
            for sid in parallel if sid in graph
        ],
    })
