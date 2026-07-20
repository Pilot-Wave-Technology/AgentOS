"""User-scoped WebSocket endpoint for sidebar badge push events.

GET /stream/user-events (WebSocket upgrade)

Each logged-in browser session opens one connection to this endpoint from
base.html. The connection stays alive for the entire session and receives
push events from the unread_listener (forwarded via user_ws_manager).

Current event types:
  unreadCountUpdated — sent when any channel's unread counter changes for
                       this user. The browser JS uses the thread_id field
                       to locate the correct [data-thread-badge] element
                       and update or hide it.

Auth follows the same pattern as the existing channel WebSockets
(goal_thread_ws, chat WS): the Firebase token is passed as the `token`
query parameter. This avoids cookie-based auth complexity on WS upgrades
and is consistent with every other WS endpoint in this codebase.

A ping frame is sent every 30 s to keep the connection alive through
load balancers and Cloud Run's idle-connection timeout (default 60 s).
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth import verify_firebase_token
from app.user_ws_manager import user_ws_manager

_log = logging.getLogger(__name__)

router = APIRouter()

# Interval between server-initiated ping frames (seconds).
# Must be below Cloud Run's idle WS timeout (~60 s) to keep the connection alive.
_PING_INTERVAL = 30


@router.websocket("/stream/user-events")
async def user_event_stream(websocket: WebSocket, token: str = "") -> None:
    """User-scoped WebSocket for real-time sidebar badge updates.

    Stays open for the lifetime of the browser session. All unread count
    changes for this user are pushed here as JSON frames, regardless of which
    goal or channel page the user is currently viewing.

    Ping frames are sent every 30 s to prevent proxy-level idle disconnects.
    The browser reconnects automatically (see base.html connectUserStream()).
    """
    # --- Auth ---
    # Verify the Firebase JWT before accepting the upgrade.
    # We accept first, then close with a 4401 code on auth failure — this is
    # required because WebSocket handshake errors don't carry HTTP bodies that
    # the browser JS can inspect meaningfully.
    await websocket.accept()

    try:
        claims = verify_firebase_token(token)
        uid: str = claims["uid"]
    except Exception as exc:
        _log.warning(
            "user-stream: auth failed (token=%s…): %s",
            (token or "")[:20], exc,
        )
        await websocket.send_json({"type": "error", "message": "Unauthorized"})
        await websocket.close(code=4401)
        return

    _log.info("user-stream: uid=%s connected", uid[:12])
    user_ws_manager.connect(uid, websocket)

    try:
        # Ping loop: keep the connection alive and handle clean browser closes.
        # We don't read from the browser (this is a server-push-only socket),
        # so iter_text() would block. Instead we sleep and catch disconnect.
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            try:
                await websocket.send_json({"type": "ping"})
                _log.debug("user-stream: ping sent uid=%s", uid[:12])
            except Exception:
                # Browser closed mid-ping; let the outer handler clean up.
                break

    except WebSocketDisconnect:
        _log.info("user-stream: uid=%s disconnected (clean close)", uid[:12])

    except Exception as exc:
        _log.warning("user-stream: uid=%s unexpected error: %s", uid[:12], exc)

    finally:
        user_ws_manager.disconnect(uid, websocket)
        _log.info("user-stream: uid=%s removed from registry", uid[:12])
