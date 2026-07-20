"""Security Council channel — human-observable feed of security reports.

A dedicated thread per goal (`goals.security_thread_id`) that receives the
Security Council's scan reports, delivered via
`app.services.orchestrator_delivery.persist_review_to_orchestrator`. Humans
can observe (read-only); the council never receives replies here.

Structurally a clone of `app/routers/orchestrator.py`'s channel skeleton
(subscriber registry + page + websocket), scoped to `security_thread_id`
instead of `orchestrator_thread_id`.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role, verify_firebase_token
from app.database import get_pool

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ── Security Council channel subscriber registry ─────────────────────────────
_security_subscribers: dict[str, set] = {}


def _subscribe_to_security(goal_id: str, send_fn):
    if goal_id not in _security_subscribers:
        _security_subscribers[goal_id] = set()
    _security_subscribers[goal_id].add(send_fn)

    def unsubscribe():
        _security_subscribers.get(goal_id, set()).discard(send_fn)
        if goal_id in _security_subscribers and not _security_subscribers[goal_id]:
            del _security_subscribers[goal_id]

    return unsubscribe


async def _broadcast_to_security(goal_id: str, message: str):
    subscribers = _security_subscribers.get(goal_id, set()).copy()
    for send_fn in subscribers:
        try:
            await send_fn(message)
        except Exception:
            _security_subscribers.get(goal_id, set()).discard(send_fn)


# ── Security Council channel page (read-only for humans) ─────────────────────

@router.get("/goals/{pid}/security", response_class=HTMLResponse)
async def security_thread_page(
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

    security_thread_id = goal.get("security_thread_id", "")
    if not security_thread_id:
        raise HTTPException(status_code=404, detail="Security Council channel not found")

    msg_rows = await pool.fetch(
        """SELECT m.id, m.sender, m.role, m.content, m.created_at, m.card_data
           FROM messages m
           WHERE m.thread_id = $1
             AND m.kind <> 'system_inject'
             AND NOT (m.kind = 'system_wake' AND m.sender = 'system' AND m.card_data IS NULL)
           ORDER BY m.created_at""",
        security_thread_id,
    )
    messages = []
    for r in msg_rows:
        m = dict(r)
        # card_data is JSONB — asyncpg returns it as a JSON string; parse so the
        # template can read card fields directly (review cards).
        if isinstance(m.get("card_data"), str) and m["card_data"]:
            try:
                m["card_data"] = json.loads(m["card_data"])
            except Exception:
                m["card_data"] = None
        messages.append(m)

    firebase_token = request.cookies.get("firebase_token", "")

    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "security-intro")

    return templates.TemplateResponse(
        "security_thread/index.html",
        {
            "request": request, "user": user, "goal": goal, "pid": pid,
            "thread_id": security_thread_id, "messages": messages,
            "firebase_token": firebase_token,
            "auto_fire_tour": auto_fire_tour,
        },
    )


# ── WebSocket (read-only broadcast to human observers) ────────────────────────

@router.websocket("/goals/{pid}/security/ws")
async def security_thread_ws(pid: str, websocket: WebSocket, token: str):
    """WebSocket for observing the Security Council channel. Humans can watch but not send."""
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

    unsubscribe = _subscribe_to_security(pid, websocket.send_text)
    _log.info("Security Council subscriber added: goal=%s, user=%s", pid, user["uid"])

    try:
        while True:
            # Human can't send messages — just keep connection alive
            await websocket.receive_text()
            # Ignore any messages from humans (read-only channel)
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
        _log.info("Security Council subscriber removed: goal=%s, user=%s", pid, user["uid"])
