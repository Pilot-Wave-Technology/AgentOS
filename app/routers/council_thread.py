"""Goal Review Council channel — human-observable feed of council reports.

A dedicated thread per goal (`goals.council_thread_id`) that receives the
Goal Review Council's story/goal/convener-failure reports, delivered via
`app.services.orchestrator_delivery.persist_review_to_orchestrator`. Humans
can observe (read-only); the council never receives replies here.

Structurally a clone of `app/routers/orchestrator.py`'s channel skeleton
(subscriber registry + page + websocket), scoped to `council_thread_id`
instead of `orchestrator_thread_id`.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role, verify_firebase_token
from app.database import get_pool
from app.routers.thread_paging import fetch_channel_page, register_channel_earlier_route

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Visual identity for this channel's message rows, passed to the shared
# channels/_message.html partial.
_ACCENT = "amber"

# ── Goal Review Council channel subscriber registry ───────────────────────────
_council_subscribers: dict[str, set] = {}


def _subscribe_to_council(goal_id: str, send_fn):
    if goal_id not in _council_subscribers:
        _council_subscribers[goal_id] = set()
    _council_subscribers[goal_id].add(send_fn)

    def unsubscribe():
        _council_subscribers.get(goal_id, set()).discard(send_fn)
        if goal_id in _council_subscribers and not _council_subscribers[goal_id]:
            del _council_subscribers[goal_id]

    return unsubscribe


async def _broadcast_to_council(goal_id: str, message: str):
    subscribers = _council_subscribers.get(goal_id, set()).copy()
    for send_fn in subscribers:
        try:
            await send_fn(message)
        except Exception:
            _council_subscribers.get(goal_id, set()).discard(send_fn)


# ── Goal Review Council channel page (read-only for humans) ──────────────────

@router.get("/goals/{pid}/council", response_class=HTMLResponse)
async def council_thread_page(
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

    council_thread_id = goal.get("council_thread_id", "")
    if not council_thread_id:
        raise HTTPException(status_code=404, detail="Goal Review Council channel not found")

    # Only the newest window is rendered; older history is paged in on demand.
    # See app/routers/thread_paging.py for why this is keyset-based.
    messages, has_older = await fetch_channel_page(pool, council_thread_id)
    _log.info(
        "council-channel: rendering goal=%s messages=%d has_older=%s",
        pid, len(messages), has_older,
    )

    firebase_token = request.cookies.get("firebase_token", "")

    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "council-intro")

    return templates.TemplateResponse(
        "council_thread/index.html",
        {
            "request": request, "user": user, "goal": goal, "pid": pid,
            "thread_id": council_thread_id, "messages": messages,
            "firebase_token": firebase_token,
            "auto_fire_tour": auto_fire_tour,
            "has_older": has_older,
            "oldest_message_id": messages[0]["id"] if messages else "",
            "accent": _ACCENT,
            "label_mode": "sender",
            "load_earlier_url": f"/goals/{pid}/council/messages/earlier",
        },
    )


register_channel_earlier_route(
    router,
    channel_slug="council",
    thread_id_column="council_thread_id",
    accent=_ACCENT,
    label_mode="sender",
)


# ── WebSocket (read-only broadcast to human observers) ────────────────────────

@router.websocket("/goals/{pid}/council/ws")
async def council_thread_ws(pid: str, websocket: WebSocket, token: str):
    """WebSocket for observing the Goal Review Council channel. Humans can watch but not send."""
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

    unsubscribe = _subscribe_to_council(pid, websocket.send_text)
    _log.info("Goal Review Council subscriber added: goal=%s, user=%s", pid, user["uid"])

    try:
        while True:
            # Human can't send messages — just keep connection alive
            await websocket.receive_text()
            # Ignore any messages from humans (read-only channel)
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
        _log.info("Goal Review Council subscriber removed: goal=%s, user=%s", pid, user["uid"])
