"""GO Message Batcher — collects concurrent messages before sending to GO.

When multiple users type in the goal thread within a quiet window (default 10s),
their messages are batched into a single message to GO. This prevents GO from
responding to each message individually and ensures it sees the full context.

Single messages are sent immediately (no delay).
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

_log = logging.getLogger(__name__)


@dataclass
class BufferedMessage:
    sender_uid: str
    sender_name: str
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class GOMessageBatcher:
    """Per-goal message batcher.

    Usage:
        batcher = GOMessageBatcher(goal_id, send_fn, quiet_window=10.0)
        await batcher.add_message(uid, name, content)
        # After 10s of no new messages, send_fn is called with the batch
    """

    def __init__(
        self,
        goal_id: str,
        send_fn: Callable[[str], any],  # async fn that sends formatted message to GO
        quiet_window: float = 10.0,
    ):
        self.goal_id = goal_id
        self._send_fn = send_fn
        self._quiet_window = quiet_window
        self._buffer: list[BufferedMessage] = []
        self._timer: Optional[asyncio.TimerHandle] = None
        self._lock = asyncio.Lock()

    async def add_message(self, sender_uid: str, sender_name: str, content: str):
        """Add a message to the buffer. Resets the quiet window timer."""
        self._buffer.append(BufferedMessage(
            sender_uid=sender_uid,
            sender_name=sender_name,
            content=content,
        ))

        # Reset timer
        if self._timer:
            self._timer.cancel()

        if len(self._buffer) == 1:
            # First message — short grace period (1.5s) to catch near-simultaneous messages
            # If no one else types within 1.5s, flush immediately
            self._timer = asyncio.get_event_loop().call_later(
                1.5,
                lambda: asyncio.create_task(self._flush()),
            )
        else:
            # Multiple messages arriving — use full quiet window to collect more
            self._timer = asyncio.get_event_loop().call_later(
                self._quiet_window,
                lambda: asyncio.create_task(self._flush()),
            )

        _log.debug("Batcher: %d messages buffered for goal %s (delay=%.1fs)",
                    len(self._buffer), self.goal_id,
                    1.5 if len(self._buffer) == 1 else self._quiet_window)

    async def flush_now(self):
        """Force-flush the buffer immediately (e.g., on system event)."""
        if self._timer:
            self._timer.cancel()
            self._timer = None
        await self._flush()

    async def _flush(self):
        """Send buffered messages to GO as a single batch."""
        async with self._lock:
            if not self._buffer:
                return

            batch = self._buffer.copy()
            self._buffer.clear()
            self._timer = None

        formatted = self._format_batch(batch)

        try:
            await self._send_fn(formatted)
            _log.info("Batcher: flushed %d messages for goal %s", len(batch), self.goal_id)
        except Exception as exc:
            _log.error("Batcher: flush failed for goal %s: %s", self.goal_id, exc)

    def _format_batch(self, batch: list[BufferedMessage]) -> str:
        """Format buffered messages for GO."""
        if len(batch) == 1:
            msg = batch[0]
            return f"[{msg.sender_name}]: {msg.content}"

        lines = []
        for m in batch:
            time_str = m.timestamp.strftime("%H:%M:%S")
            lines.append(f"[{m.sender_name} at {time_str}]: {m.content}")

        return (
            f"{len(batch)} messages from the team:\n\n"
            + "\n".join(lines)
            + "\n\nAddress all points in your response."
        )

    @property
    def pending_count(self) -> int:
        return len(self._buffer)


# ── Global batcher registry ─────────────────────────────────────────────────
# One batcher per goal, created on demand

_batchers: dict[str, GOMessageBatcher] = {}


def get_or_create_batcher(
    goal_id: str,
    send_fn: Callable,
    quiet_window: float = 10.0,
) -> GOMessageBatcher:
    """Get or create a batcher for a goal."""
    if goal_id not in _batchers:
        _batchers[goal_id] = GOMessageBatcher(goal_id, send_fn, quiet_window)
    return _batchers[goal_id]


def remove_batcher(goal_id: str):
    """Remove a batcher when the goal session ends."""
    _batchers.pop(goal_id, None)
