"""User-scoped WebSocket registry for cross-channel badge push.

Each logged-in browser session opens one WebSocket to /stream/user-events.
This module maintains the in-memory map of uid → [WebSocket] so that the
unread_listener can push unreadCountUpdated events to any user who is currently
online, regardless of which channel page they are viewing.

A user can have multiple tabs open — each tab gets its own WebSocket. Events
are fanned out to all of them. Dead connections are pruned silently on failed
send.

This is intentionally a thin, stateless registry. Persistence (the actual
unread count) lives in thread_participants in Postgres. If this process
restarts, the registry is empty until users reconnect — which is fine because
on reconnect the sidebar re-renders with the current DB count.
"""

import logging
from collections import defaultdict

from fastapi import WebSocket

_log = logging.getLogger(__name__)


class UserWSManager:
    """Registry of live user-scoped WebSocket connections.

    Thread-safety note: asyncio is single-threaded so no locks are needed.
    All mutations happen in the same event loop that drives FastAPI.
    """

    def __init__(self) -> None:
        # uid → list of open WebSocket connections for that user.
        self._conns: dict[str, list[WebSocket]] = defaultdict(list)

    def connect(self, uid: str, ws: WebSocket) -> None:
        """Register a new browser WebSocket for a user."""
        self._conns[uid].append(ws)
        _log.info(
            "user-ws: connected uid=%s open_tabs=%d",
            uid[:12], len(self._conns[uid]),
        )

    def disconnect(self, uid: str, ws: WebSocket) -> None:
        """Unregister a WebSocket when the browser tab closes."""
        try:
            self._conns[uid].remove(ws)
        except ValueError:
            pass
        if not self._conns[uid]:
            del self._conns[uid]
        _log.info("user-ws: disconnected uid=%s", uid[:12])

    async def push(self, uid: str, data: dict) -> None:
        """Send a JSON event to all open tabs for a user.

        Dead connections are pruned after the send attempt so they don't
        accumulate. Errors are logged at DEBUG level — a dead socket is
        expected when tabs close without a clean WS close handshake.
        """
        conns = list(self._conns.get(uid, []))
        if not conns:
            _log.debug("user-ws: push skipped — uid=%s has no open connections", uid[:12])
            return

        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(data)
                _log.debug("user-ws: pushed %s to uid=%s", data.get("type"), uid[:12])
            except Exception as exc:
                _log.debug("user-ws: send failed uid=%s (%s) — pruning", uid[:12], exc)
                dead.append(ws)

        for ws in dead:
            self.disconnect(uid, ws)


# Singleton — imported by unread_listener.py and user_stream.py.
user_ws_manager = UserWSManager()
