"""Cross-instance fan-out via Postgres LISTEN/NOTIFY (Phase 2).

NOTIFY carries only `{thread_id, message_id}` (signal, not payload — dodges the
8 KB NOTIFY cap and scales). Each web instance LISTENs, fetches the row, and
re-broadcasts to its *local* subscribers. Decoupled from the web tier: the
delivery sink is a callback registered via `set_broadcaster`.

Uses a dedicated connection (not the shared pool) so a long-lived LISTEN
doesn't tie up a pool slot. Auto-reconnects.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import asyncpg

from .config import get_chat_config
from .repository import CHANNEL_MSG

_log = logging.getLogger(__name__)

# broadcaster(thread_id, message_row: dict) -> Awaitable[None]
Broadcaster = Callable[[str, dict], Awaitable[None]]
_broadcaster: Optional[Broadcaster] = None


def set_broadcaster(fn: Broadcaster) -> None:
    """Wire the local delivery sink (web tier provides one that pushes to its
    in-process subscribers)."""
    global _broadcaster
    _broadcaster = fn


class FanoutListener:
    def __init__(self) -> None:
        self._conn: Optional[asyncpg.Connection] = None
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever())
        _log.info("chat_module: fan-out listener started")

    async def stop(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                await self._conn.remove_listener(CHANNEL_MSG, self._on_notify)
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._task:
            self._task.cancel()

    async def _run_forever(self) -> None:
        cfg = get_chat_config()
        backoff = 1
        while not self._stop.is_set():
            try:
                self._conn = await asyncpg.connect(cfg.database_url)
                await self._conn.add_listener(CHANNEL_MSG, self._on_notify)
                backoff = 1
                _log.info("chat_module: fan-out LISTEN %s active", CHANNEL_MSG)
                while not self._stop.is_set():
                    await asyncio.sleep(5)
                    # Liveness ping; raises if the connection dropped.
                    await self._conn.execute("SELECT 1")
            except Exception:
                _log.warning("chat_module: fan-out listener reconnecting in %ds", backoff,
                             exc_info=True)
                if self._conn is not None:
                    try:
                        await self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _on_notify(self, conn, pid, channel, payload: str) -> None:
        # add_listener callbacks are sync; hand off to the loop.
        asyncio.create_task(self._handle(payload))

    async def _handle(self, payload: str) -> None:
        if _broadcaster is None:
            return
        try:
            data = json.loads(payload)
            thread_id = data["thread_id"]
            message_id = data["message_id"]
        except Exception:
            return
        from app.database import get_pool
        row = await get_pool().fetchrow(
            """
            SELECT id, thread_id, sender, role, content, token_count, created_at, platform,
                   card_data, kind
            FROM messages WHERE id = $1
              AND kind <> 'system_inject'
            """,
            message_id,
        )
        # THE SYSTEM-WAKE FILTER MOVED OUT OF THE SQL, because it is not
        # a property of the row -- it is a property of the THREAD.
        #
        # On an autonomous errand's own thread these rows ARE the
        # conversation: the parks, the check-ins, the kernel's answers.
        # GO is woken by them, acts, and reports; hiding them leaves a
        # person watching work happen with no account of why, which is
        # the black box the whole feature was meant not to be.
        #
        # Everywhere else -- a goal's main thread, an assistant's chat, a
        # council -- the same rows are plumbing and burying the
        # conversation in them would be a plain regression. So the test
        # is the grant, read per thread, and revoking it makes the thread
        # quiet again on the very next message.
        if (row is not None and row["kind"] == "system_wake"
                and row["sender"] == "system" and row["card_data"] is None):
            from app.services.errand_thread_visibility import (
                is_autonomous_errand_thread)
            if not await is_autonomous_errand_thread(get_pool(), thread_id):
                return
        if not row:
            # No row, or an internal turn: system_inject (context, rides next
            # turn) or a system-sender system_wake with no card (the memory
            # flush). Those never render as a chat bubble. Task-controller
            # system_wake updates (sender='task-controller') are NOT filtered —
            # they stay visible. A card-bearing row (card_data present) is a
            # visible bubble even when its kind became system_wake (task cards
            # forwarded to the assistant), so it is NOT filtered.
            return
        try:
            await _broadcaster(thread_id, dict(row))
        except Exception:
            _log.warning("chat_module: broadcaster failed for thread %s", thread_id,
                         exc_info=True)
