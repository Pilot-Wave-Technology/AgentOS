"""Background Postgres LISTEN loop for real-time unread badge pushes.

When a message is inserted (via app/unread.py:increment_unread), a
pg_notify('unread_updates', payload) fires. This module maintains a dedicated
asyncpg connection that listens on that channel and dispatches each notification
to the affected user's open WebSocket connections via user_ws_manager.

WHY a dedicated connection (not from the pool):
  Pooled connections are checked in/out per-request and do not survive long
  enough to LISTEN persistently. asyncpg's add_listener() requires a connection
  that stays open for the lifetime of the subscription. A dedicated connection
  outside the pool is the correct pattern.

WHY it auto-reconnects:
  Cloud SQL connections can be reset (proxy restart, idle timeout, maintenance).
  The 5 s retry loop ensures the listener recovers without restarting the
  Cloud Run instance.
"""

import asyncio
import json
import logging

import asyncpg

from app.user_ws_manager import user_ws_manager

_log = logging.getLogger(__name__)

# Keepalive interval in seconds. Sends a no-op SELECT to prevent the Cloud SQL
# Auth Proxy from treating the connection as idle and closing it.
_KEEPALIVE_INTERVAL = 30

# Backoff between reconnect attempts after a connection loss.
_RECONNECT_DELAY = 5


async def start_unread_listener(dsn: str) -> None:
    """Run forever. Connects to Postgres, listens on 'unread_updates', and
    dispatches notifications to user WebSocket connections.

    Called as an asyncio.create_task from main.py on_startup. Errors are
    logged at WARNING level and never propagate — a listener failure must
    not crash the web process. The worst case is that badge updates fall
    back to the sidebar's server-rendered initial state until the listener
    reconnects.
    """
    _log.info("unread-listener: starting (dsn=redacted)")

    while True:
        conn = None
        try:
            conn = await asyncpg.connect(dsn)
            _log.info("unread-listener: connected — subscribing to '%s'", "unread_updates")

            async def _on_notify(
                connection: asyncpg.Connection,
                pid: int,
                channel: str,
                payload: str,
            ) -> None:
                """Dispatch a single pg_notify payload to the affected user's WS."""
                try:
                    data = json.loads(payload)
                    uid = data.get("uid", "")
                    thread_id = data.get("thread_id", "")
                    count = data.get("count", 0)

                    if not uid or not thread_id:
                        _log.warning(
                            "unread-listener: malformed payload (missing uid/thread_id): %s",
                            payload[:200],
                        )
                        return

                    _log.debug(
                        "unread-listener: dispatching uid=%s thread=%s count=%d",
                        uid[:12], thread_id[:12], count,
                    )

                    await user_ws_manager.push(uid, {
                        "type":      "unreadCountUpdated",
                        "thread_id": thread_id,
                        "count":     count,
                    })

                except json.JSONDecodeError:
                    _log.warning(
                        "unread-listener: invalid JSON payload: %s", payload[:200],
                    )
                except Exception as exc:
                    _log.warning(
                        "unread-listener: dispatch error for payload=%s: %s",
                        payload[:100], exc,
                    )

            await conn.add_listener("unread_updates", _on_notify)
            _log.info("unread-listener: listening — badge updates are live")

            # Keepalive loop: holds the connection open and pings Postgres every
            # _KEEPALIVE_INTERVAL seconds so Cloud SQL doesn't drop the idle conn.
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                await conn.execute("SELECT 1")
                _log.debug("unread-listener: keepalive ping sent")

        except asyncio.CancelledError:
            _log.info("unread-listener: cancelled — shutting down")
            break

        except Exception as exc:
            _log.warning(
                "unread-listener: connection lost (%s) — reconnecting in %ds",
                exc, _RECONNECT_DELAY,
            )

        finally:
            if conn is not None:
                try:
                    await conn.close()
                    _log.debug("unread-listener: connection closed")
                except Exception:
                    pass

        await asyncio.sleep(_RECONNECT_DELAY)
