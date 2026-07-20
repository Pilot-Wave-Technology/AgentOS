"""Goal thread — shared discussion between all goal members and the GO agent.

The goal thread is the single place where humans discuss planning, decisions,
and status with the Goal Orchestrator. All members see all messages in real-time.
Messages are forwarded to the GO agent's OpenClaw session for processing.
"""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role, verify_firebase_token
from app.config import get_settings
from app.database import get_pool
from app.notifications import push_notification
from app.openclaw import session_manager, gateway_admin
from app.unread import increment_unread, mark_thread_read
from app.ledger.journal import append_event
from app.ledger.schema import MESSAGE_POSTED
from app.ledger.actors import agent_actor
from app.services.go_agent import build_go_context
from app.services.go_response import process_go_response
from app.services.go_batcher import get_or_create_batcher
from app.services.go_history import maybe_update_goal_summary

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ── Goal thread subscriber registry (multi-user broadcast) ──────────────────
# Maps goal_id → set of websocket.send_text functions
_goal_thread_subscribers: dict[str, set] = {}


def _subscribe_to_goal_thread(goal_id: str, send_fn):
    """Register a websocket send function for goal thread broadcasts."""
    if goal_id not in _goal_thread_subscribers:
        _goal_thread_subscribers[goal_id] = set()
    _goal_thread_subscribers[goal_id].add(send_fn)

    def unsubscribe():
        _goal_thread_subscribers.get(goal_id, set()).discard(send_fn)
        if goal_id in _goal_thread_subscribers and not _goal_thread_subscribers[goal_id]:
            del _goal_thread_subscribers[goal_id]

    return unsubscribe


async def _broadcast_to_goal_thread(goal_id: str, message: str):
    """Send a message to all connected goal thread subscribers."""
    subscribers = _goal_thread_subscribers.get(goal_id, set()).copy()
    for send_fn in subscribers:
        try:
            await send_fn(message)
        except Exception:
            _goal_thread_subscribers.get(goal_id, set()).discard(send_fn)


# ── Goal thread page ────────────────────────────────────────────────────────

@router.get("/goals/{pid}/thread", response_class=HTMLResponse)
async def goal_thread_page(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Render the shared goal thread page."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()

    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1 AND status = 'active'", pid)
    if not goal:
        raise HTTPException(status_code=404)
    goal = dict(goal)

    go_thread_id = goal.get("go_thread_id", "")
    if not go_thread_id:
        raise HTTPException(status_code=404, detail="Goal thread not found")

    # Mark the GO thread as read for this user — zeroes their unread counter
    # and notifies any other open tabs via pg_notify → user WS.
    # Non-critical: wrapped so a counter failure never breaks page load.
    try:
        await mark_thread_read(pool, go_thread_id, user["uid"])
    except Exception as exc:
        _log.warning("goal-thread: mark_thread_read failed (non-fatal): %s", exc)

    # Load messages (join users for humans, agents for GO)
    msg_rows = await pool.fetch(
        """SELECT m.id, m.sender, m.role, m.content, m.created_at, m.token_count,
                  COALESCE(u.display_name, a.name, m.sender) AS sender_name
           FROM messages m
           LEFT JOIN users u ON u.uid = m.sender
           LEFT JOIN agents a ON a.id = m.sender
           WHERE m.thread_id = $1
             AND m.sender <> 'system'
           ORDER BY m.created_at""",
        go_thread_id,
    )
    messages = []
    for r in msg_rows:
        msg = dict(r)
        # Clean up GO display name: "GO — Goal Name" → "Goal Orchestrator"
        if msg["role"] == "agent" and msg["sender_name"].startswith("GO —"):
            msg["sender_name"] = "Goal Orchestrator"
        messages.append(msg)

    # Load members for display
    member_rows = await pool.fetch(
        """SELECT gm.uid, gm.role, u.display_name, u.email
           FROM goal_members gm
           JOIN users u ON u.uid = gm.uid
           WHERE gm.goal_id = $1""",
        pid,
    )
    members = [dict(r) for r in member_rows]

    firebase_token = request.cookies.get("firebase_token", "")

    # Skipped for the reserved __platform__ goal: its agent is the Platform
    # Steward, not a per-goal GO, and the tour's copy talks specifically
    # about GO.
    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = (
        await check_and_mark_milestone_if_enabled(user, "goal-thread-intro")
        if pid != "__platform__" else False
    )

    return templates.TemplateResponse(
        "goal_thread/index.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "thread_id": go_thread_id,
            "messages": messages,
            "members": members,
            "firebase_token": firebase_token,
            # Gate the Security Council manual-trigger button: hidden when the
            # manual trigger is disabled on this deployment (e.g. dormant on prod).
            "security_enabled": getattr(get_settings(), "security_ui_enabled", False),
            "auto_fire_tour": auto_fire_tour,
        },
    )


# ── Run state (is GO currently thinking / using a tool?) ────────────────────

@router.get("/goals/{pid}/thread/run-state")
async def get_go_thread_run_state(
    pid: str,
    user: dict = Depends(get_current_user),
):
    """Return the GO's current run indicator state for this goal's thread.

    Used by the GO thread UI on page load to restore the "thinking…" or
    "Using <tool>…" indicator if a run is still in flight.

    Stale rows (updated_at > 30 min ago) are cleaned up lazily.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    from fastapi.responses import JSONResponse
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT go_thread_id FROM goals WHERE id = $1", pid,
    )
    if not goal or not goal["go_thread_id"]:
        return JSONResponse({"status": "idle"})
    thread_id = goal["go_thread_id"]
    await pool.execute(
        "DELETE FROM chat_run_state WHERE updated_at < now() - interval '30 minutes'",
    )
    row = await pool.fetchrow(
        """SELECT status, current_tool, started_at
             FROM chat_run_state
            WHERE thread_id = $1
              AND updated_at > now() - interval '30 minutes'""",
        thread_id,
    )
    if not row:
        return JSONResponse({"status": "idle"})
    return JSONResponse({
        "status": row["status"],
        "tool": row["current_tool"] or "",
        "started_at": row["started_at"].isoformat() if row["started_at"] else "",
    })


@router.get("/goals/{pid}/thread/messages")
async def get_thread_messages_after(
    pid: str,
    after: str = "",
    user: dict = Depends(get_current_user),
):
    """Return messages in the GO thread that arrived after `after` (message ID).

    Used by the browser on WS reconnect to catch up on any messages that were
    broadcast while the connection was down. Returns up to 50 messages to
    bound response size.

    If `after` is empty or not found in the thread, returns an empty list.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    from fastapi.responses import JSONResponse
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT go_thread_id FROM goals WHERE id = $1", pid,
    )
    if not goal or not goal["go_thread_id"]:
        return JSONResponse({"messages": []})
    thread_id = goal["go_thread_id"]
    if not after:
        return JSONResponse({"messages": []})

    # Find the creation time of the anchor message so we can fetch anything after it.
    anchor = await pool.fetchrow(
        "SELECT created_at FROM messages WHERE id = $1 AND thread_id = $2",
        after, thread_id,
    )
    if not anchor:
        return JSONResponse({"messages": []})

    rows = await pool.fetch(
        """SELECT m.id, m.sender, m.role, m.content, m.created_at,
                  COALESCE(u.display_name, a.name, m.sender) AS sender_name
             FROM messages m
             LEFT JOIN users u ON u.uid = m.sender
             LEFT JOIN agents a ON a.id = m.sender
            WHERE m.thread_id = $1
              AND m.created_at > $2
              AND m.sender <> 'system'
            ORDER BY m.created_at ASC
            LIMIT 50""",
        thread_id, anchor["created_at"],
    )
    return JSONResponse({
        "messages": [
            {
                "id": r["id"],
                "sender": r["sender"],
                "sender_name": r["sender_name"],
                "role": r["role"],
                "content": r["content"],
            }
            for r in rows
        ]
    })


# ── WebSocket endpoint ──────────────────────────────────────────────────────

@router.websocket("/goals/{pid}/thread/ws")
async def goal_thread_ws(
    pid: str,
    websocket: WebSocket,
    token: str,
):
    """WebSocket for the shared goal thread.

    All connected members see each other's messages in real-time.
    Messages are also forwarded to the GO agent for processing.
    """
    await websocket.accept()

    # Auth
    try:
        claims = verify_firebase_token(token)
        uid = claims["uid"]
    except HTTPException:
        await websocket.send_text(json.dumps({"type": "error", "message": "Unauthorized"}))
        await websocket.close()
        return

    pool = get_pool()

    # Verify membership
    role = await get_goal_role(uid, pid)
    if role is None:
        await websocket.send_text(json.dumps({"type": "error", "message": "Not a member"}))
        await websocket.close()
        return

    # Get goal + GO info
    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    if not goal:
        await websocket.close()
        return
    # Only active goals get a GO session / lazy GO create (status column, not state).
    if goal.get("status") != "active":
        _log.info("goal_thread ws: goal %s not active (status=%s) — refusing GO session/lazy-create",
                  pid, goal.get("status"))
        await websocket.close()
        return

    go_thread_id = goal["go_thread_id"]
    go_agent_id = goal["go_agent_id"]
    user_row = await pool.fetchrow("SELECT * FROM users WHERE uid = $1", uid)
    user_actor_id = user_row["actor_id"] if user_row else None
    sender_name = (user_row["display_name"] if user_row else None) or ""

    if not go_agent_id:
        # GO creation at goal-creation is best-effort and was historically lost to a
        # gateway readiness race ("unknown agent id"), leaving the goal GO-less with no
        # retry anywhere. Self-heal: (re)create the GO on thread open. create_go_for_goal
        # is idempotent (reuses an existing gateway agent), and it now waits for the
        # gateway to register the agent before pushing files.
        try:
            from app.services.go_agent import create_go_for_goal
            from app.llm_models import default_for
            go_result = await create_go_for_goal(
                goal_id=pid,
                goal_name=goal.get("name") or pid,
                objective=goal.get("objective") or "",
                created_by_uid=uid,
                model=goal.get("go_model") or default_for("go"),
            )
            go_agent_id = go_result["agent_id"]
            await pool.execute(
                "UPDATE goals SET go_agent_id = $1 WHERE id = $2", go_agent_id, pid,
            )
            _log.info("GO agent lazily created for goal %s on thread open: %s",
                      pid, go_agent_id)
        except Exception as exc:
            _log.warning(
                "Lazy GO creation failed for goal %s: %s — thread will work but GO "
                "won't respond (retries next thread open)", pid, exc,
            )

    # Build GO session context and callbacks
    go_context = await _get_go_context(pool, pid, go_agent_id)
    on_message, on_notify = _make_go_callbacks(pool, pid, go_agent_id, go_thread_id)

    # Get or create GO session (reused across all members in this goal)
    session = None
    if go_agent_id:
        session = session_manager.get_or_create(
            pid, go_agent_id, go_context, on_message, on_notify,
            thread_id=go_thread_id,
        )
        # Set gateway agent ID so send_to_gateway uses it for session key
        gw_id = go_context.get("gateway_agent_id", "")
        if gw_id:
            session._gateway_agent_id = gw_id
            _log.info("GO session: agent_id=%s, gw_id=%s, thread=%s", go_agent_id, gw_id, go_thread_id)

            # NOTE: We deliberately do NOT re-push AGENTS.md (or any
            # other static bootstrap file) on WS open. Those files are
            # written ONCE at agent provisioning. Re-pushing them on
            # every chat reconnect invalidates Gemini's implicit
            # prompt cache, costing 15-20K tokens of re-uploaded
            # `input` per reconnect. The only file that legitimately
            # changes mid-session is RESOURCES.md, which has its own
            # change-driven push via _notify_resources_refreshed (with
            # hash-dedupe). If the GO template's AGENTS.md is edited
            # in the admin UI, the template_sync push-on-write path
            # propagates that to live agents — no belt-and-braces
            # needed here.
        else:
            _log.warning("GO session: no gateway_agent_id found for %s", go_agent_id)

    # Mark the GO thread as read for this user on WS connect (they are now viewing it).
    # Non-critical: counter failure must never block the WS connection.
    try:
        await mark_thread_read(pool, go_thread_id, uid)
    except Exception as exc:
        _log.warning("goal-thread WS: mark_thread_read failed (non-fatal): %s", exc)

    # Subscribe this websocket to goal thread broadcasts (all members see all messages)
    unsub_thread = _subscribe_to_goal_thread(pid, websocket.send_text)
    _log.info("Goal thread subscriber added: goal=%s, user=%s, total=%d",
              pid, uid, len(_goal_thread_subscribers.get(pid, set())))

    # Subscribe to the GO session's tool events so the UI can show
    # "using <tool>" in real time. We only forward tool_start / tool_result —
    # everything else still comes through _broadcast_to_goal_thread to avoid
    # duplicate message rendering.
    unsub_session = None
    if session:
        async def _tool_event_relay(raw: str):
            try:
                parsed = json.loads(raw)
                t = parsed.get("type")
                if t in ("tool_start", "tool_result"):
                    await websocket.send_text(raw)
            except Exception:
                pass

        session._subscribers.setdefault(go_thread_id, set()).add(_tool_event_relay)

        def _unsub_session():
            session._subscribers.get(go_thread_id, set()).discard(_tool_event_relay)

        unsub_session = _unsub_session
        _log.info(
            "GO session tool-event subscriber added: thread=%s",
            go_thread_id[:12],
        )

    try:
        async for raw in websocket.iter_text():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if parsed.get("type") != "message":
                continue

            content = parsed.get("content", "").strip()
            if not content:
                continue

            msg_id = str(uuid.uuid4())

            # 1. Freeze gate — check BEFORE persisting so a dropped message
            # never becomes an orphaned DB row that GO silently misses.
            try:
                from app.services.execution_control import conversation_block as _conv_block  # noqa: PLC0415
                _freeze = await _conv_block(pid)
                if _freeze is not None:
                    await websocket.send_text(json.dumps({
                        "type": "system_notice",
                        "code": "EXECUTION_PAUSED",
                        "scope": _freeze.scope,
                        "message": (
                            f"⏸ The goal is paused ({_freeze.scope}). "
                            "Resume to talk to the orchestrator again."
                        ),
                    }))
                    continue
            except Exception:
                _log.exception("goal_thread freeze gate failed — failing open")

            # 2. Persist human message to DB (after gate — no orphaned rows on freeze)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    last_event_id = await _last_thread_event_id(conn, go_thread_id)
                    await append_event(
                        conn,
                        event_type=MESSAGE_POSTED,
                        payload={
                            "thread_id": go_thread_id,
                            "message_id": msg_id,
                            "sender": uid,
                            "role": "human",
                            "content_preview": content[:200],
                        },
                        actor_id=user_actor_id,
                        correlation_id=go_thread_id,
                        causation_ids=[last_event_id] if last_event_id else None,
                        context_goal_id=pid,
                        context_conversation_id=go_thread_id,
                    )
                    await conn.execute(
                        """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                           VALUES ($1, $2, $3, 'human', $4, 0)""",
                        msg_id, go_thread_id, uid, content,
                    )

            # 2e. Increment unread for all OTHER goal members so their sidebar
            # badges update. Non-critical — wrapped so a counter failure never
            # breaks message delivery.
            try:
                await increment_unread(pool, go_thread_id, sender_uid=uid)
            except Exception as exc:
                _log.warning(
                    "goal-thread: increment_unread (human msg) failed (non-fatal): %s", exc,
                )

            # 3. Broadcast to ALL goal thread subscribers (sender + other members)
            broadcast_msg = json.dumps({
                "type": "message",
                "id": msg_id,
                "sender": uid,
                "sender_name": sender_name,
                "role": "human",
                "content": content,
            })
            await _broadcast_to_goal_thread(pid, broadcast_msg)

            # 3b. Mirror to Slack goal channel (if Slack is enabled).
            try:
                from app.channels.slack.outbound import relay_human_message
                import asyncio as _asyncio_relay
                _asyncio_relay.create_task(
                    relay_human_message(pid, go_thread_id, content, uid, msg_id=msg_id)
                )
            except ImportError:
                pass
            except Exception as exc:
                _log.warning(
                    "Slack human relay scheduling failed (goal=%s): %s", pid, exc,
                )

            # 3c. Mirror to Notion GO thread page.
            try:
                from app.channels.notion.outbound import relay_human_message as _notion_human
                import asyncio as _asyncio_notion
                _asyncio_notion.create_task(_notion_human(pid, go_thread_id, content, sender_name))
            except Exception as exc:
                _log.warning("Notion human relay scheduling failed (goal=%s): %s", pid, exc)

            # 2d. Self-heal a stale connection: if this WS connected BEFORE the GO
            # existed (session is None — go_agent_id was empty at connect) but the GO has
            # since been created (lazy-ensure on another connection), bind the shared GO
            # session NOW so this message forwards instead of being silently dropped.
            # Without this the user has to hard-refresh; with it, the next message just
            # works. get_or_create returns the existing GO session if one is live.
            if session is None:
                fresh_go_id = await pool.fetchval(
                    "SELECT go_agent_id FROM goals WHERE id = $1", pid,
                )
                if fresh_go_id:
                    go_agent_id = fresh_go_id
                    go_context = await _get_go_context(pool, pid, go_agent_id)
                    on_message, on_notify = _make_go_callbacks(
                        pool, pid, go_agent_id, go_thread_id,
                    )
                    session = session_manager.get_or_create(
                        pid, go_agent_id, go_context, on_message, on_notify,
                        thread_id=go_thread_id,
                    )
                    gw_id = go_context.get("gateway_agent_id", "")
                    if gw_id:
                        session._gateway_agent_id = gw_id
                    _log.info(
                        "GO session lazily bound on first message for goal %s "
                        "(connection was stale/session-less)", pid,
                    )

            # 3. Forward to GO. With the chat_module queue on, enqueue the RAW
            # message (the co-located dispatcher batches + adds GO context);
            # otherwise use the in-router batcher.
            from app.chat_module.config import get_chat_config as _gcc
            _queue_on = _gcc().queue_enabled
            if session and _queue_on:
                await session.send_to_gateway(
                    json.dumps({"type": "message", "content": content,
                                "thread_id": go_thread_id}),
                    persisted_message_id=msg_id,
                )
                # Show the "thinking" indicator live for all thread members.
                # The legacy in-router batcher (the elif below) broadcasts this
                # when the batch fires; the queue path bypasses the batcher, so
                # without this the loader only ever appears on refresh (via the
                # DB run-state the dispatcher writes) and never on send.
                await _broadcast_to_goal_thread(pid, json.dumps({"type": "thinking"}))
            elif session:
                async def _send_batched_to_go(formatted_message: str):
                    """Send a batched message to GO with slim context (goal + team only)."""
                    # Broadcast "thinking" when batch fires
                    await _broadcast_to_goal_thread(pid, json.dumps({"type": "thinking"}))

                    # build_go_context is now slim: only goal identity (id/name/
                    # objective/description) + team roster. Everything else
                    # (tasks, resources, memory, recent messages, state doc) is
                    # available via bootstrap files + pwt_* tools.
                    try:
                        dynamic_context = await build_go_context(pid)
                    except Exception as exc:
                        _log.warning("Failed to build GO context for %s: %s", pid, exc)
                        dynamic_context = ""

                    full_message = (
                        f"{dynamic_context}\n\n---\n\n{formatted_message}"
                        if dynamic_context else formatted_message
                    )

                    go_msg = json.dumps({
                        "type": "message",
                        "content": full_message,
                        "thread_id": go_thread_id,
                    })
                    _log.info("Sending batched message to GO: goal=%s, context_len=%d",
                              pid, len(dynamic_context))
                    await session.send_to_gateway(go_msg)

                # Get goal settings for batch window
                batch_window = 10.0  # default
                try:
                    settings = goal.get("settings") or {}
                    if isinstance(settings, str):
                        import json as _json
                        settings = _json.loads(settings)
                    batch_window = float(settings.get("batch_window_seconds", 10.0))
                except Exception:
                    pass

                batcher = get_or_create_batcher(pid, _send_batched_to_go, batch_window)
                await batcher.add_message(uid, sender_name, content)
                _log.info("Message queued in batcher (pending=%d)", batcher.pending_count)

    except WebSocketDisconnect:
        _log.debug("Goal thread WS disconnected: %s/%s", pid, uid)
    except Exception as exc:
        _log.error("Goal thread WS error: %s/%s: %s", pid, uid, exc)
    finally:
        unsub_thread()
        if unsub_session:
            unsub_session()


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_go_context(pool, pid: str, go_agent_id: str) -> dict:
    """Build the goal context dict for the GO agent session."""
    if not go_agent_id:
        return {}

    agent = await pool.fetchrow("SELECT * FROM agents WHERE id = $1", go_agent_id)
    if not agent:
        return {}

    ctx = await pool.fetchrow(
        "SELECT * FROM agent_contexts WHERE goal_id = $1 AND agent_id = $2",
        pid, go_agent_id,
    )

    return {
        "agent_id": go_agent_id,
        "gateway_agent_id": agent.get("gateway_agent_id", ""),
        "goal_id": pid,
        "agent_type": "GO",
        "scope": "goal",
        "model": agent.get("model", "google/gemini-2.5-flash"),
        "additional_instructions": ctx.get("additional_instructions", "") if ctx else "",
        "tools_allow": agent.get("tools_allow", []),
        "tools_deny": agent.get("tools_deny", []),
        "memory_enabled": True,
        "max_tokens": agent.get("max_tokens"),
        "workspace_files": ctx.get("workspace_files", []) if ctx else [],
        "skills": ctx.get("skills", []) if ctx else [],
    }


def _make_go_callbacks(pool, pid: str, go_agent_id: str, go_thread_id: str):
    """Create on_message and on_notify callbacks for the GO agent session."""

    async def on_message(thread_id: str, role: str, content: str):
        """Process GO's response: execute actions, then persist clean text."""
        if not content or not content.strip():
            return

        # 1. Parse and execute any go-action blocks, get clean text
        try:
            clean_content = await process_go_response(pid, content)
        except Exception as exc:
            _log.error("GO response processing failed for %s: %s", pid, exc)
            clean_content = content  # fall back to raw content

        # 2. Persist the clean text (action blocks stripped)
        msg_id = str(uuid.uuid4())

        async with pool.acquire() as conn:
            async with conn.transaction():
                last_event_id = await _last_thread_event_id(conn, go_thread_id)
                # The GO agent is the sender; system-typed fallback if its
                # principal row is missing (never crash on the ledger NOT NULL).
                agent_actor_id, agent_actor_type = await agent_actor(go_agent_id)
                await append_event(
                    conn,
                    event_type=MESSAGE_POSTED,
                    payload={
                        "thread_id": go_thread_id,
                        "message_id": msg_id,
                        "sender": go_agent_id,
                        "role": "agent",
                        "content_preview": clean_content[:200],
                    },
                    actor_id=agent_actor_id,
                    actor_type=agent_actor_type,
                    correlation_id=go_thread_id,
                    causation_ids=[last_event_id] if last_event_id else None,
                    context_goal_id=pid,
                    context_conversation_id=go_thread_id,
                )
                await conn.execute(
                    """INSERT INTO messages (id, thread_id, sender, role, content)
                       VALUES ($1, $2, $3, 'agent', $4)""",
                    msg_id, go_thread_id, go_agent_id, clean_content,
                )

        # 3. Broadcast final message to all goal thread subscribers
        #    This ensures all members see the response even if streaming failed
        final_msg = json.dumps({
            "type": "message",
            "id": msg_id,
            "sender": go_agent_id,
            "sender_name": "Goal Orchestrator",
            "role": "agent",
            "content": clean_content,
        })
        await _broadcast_to_goal_thread(pid, final_msg)

        # 3b. Increment unread for all goal members — the GO agent id is not a
        # human uid so the WHERE uid != $2 clause effectively matches everyone.
        # Non-critical: counter failure must never block the GO reply path.
        try:
            await increment_unread(pool, go_thread_id, sender_uid=go_agent_id)
        except Exception as exc:
            _log.warning(
                "goal-thread: increment_unread (GO reply) failed (non-fatal): %s", exc,
            )

        # 4. Mirror to all registered outbound channels (Slack, Notion, etc.).
        #    Skipped if clean_content is empty — means the whole response was
        #    action markup (e.g. the GO replied via reply_assistant only).
        if clean_content and clean_content.strip():
            from app.channels.base import relay_all_outbound
            await relay_all_outbound(pid, go_thread_id, clean_content, msg_id)

        # Refresh the rolling per-goal summary in the background (non-blocking;
        # self-gates at GO_SUMMARY_MSG_THRESHOLD GO-sent messages).
        import asyncio
        asyncio.create_task(_safe_update_goal_summary(pid))

        # Return the persisted message id so callers (openclaw.py) can
        # ship it in the token_stream_end broadcast.
        return msg_id

    async def on_notify(thread_id: str, agent_id: str, content: str):
        """Notify all goal members when GO posts a response."""
        member_uids = await pool.fetch(
            "SELECT uid FROM goal_members WHERE goal_id = $1", pid,
        )
        for row in member_uids:
            try:
                await push_notification(
                    row["uid"],
                    type="go_message",
                    title="Goal Orchestrator responded",
                    body=content[:120],
                    goal_id=pid,
                    thread_id=go_thread_id,
                    link=f"/goals/{pid}/thread",
                )
            except Exception:
                pass

    return on_message, on_notify


async def _safe_update_goal_summary(goal_id: str):
    """Background task: refresh the rolling per-goal summary if threshold reached.

    Best-effort and fully isolated — a failure here must never touch GO's
    reply path.
    """
    try:
        await maybe_update_goal_summary(goal_id)
    except Exception as exc:
        _log.debug("Goal summary update skipped for %s: %s", goal_id, exc)


async def _last_thread_event_id(conn, thread_id: str) -> str | None:
    """Get the most recent ledger event_id for this thread (for causation chain)."""
    row = await conn.fetchrow(
        """SELECT event_id FROM ledger_events
           WHERE context_conversation_id = $1
           ORDER BY timestamp DESC LIMIT 1""",
        thread_id,
    )
    return str(row["event_id"]) if row else None
