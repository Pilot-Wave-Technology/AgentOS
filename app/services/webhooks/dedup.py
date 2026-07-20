"""Generic webhook delivery-ID deduplication helper.

Shared by the git webhook receiver and any future inbound channel adapters.
Pattern matches the Notion inbound adapter's delivery_id check so both channels
reuse one dedup mechanism rather than implementing separately.

Usage:
    if await is_duplicate(pool, "git", delivery_id):
        return  # already processed

    # ... process event ...

    await mark_processed(pool, "git", delivery_id)
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# How many days to keep delivery records before they are eligible for cleanup.
# Providers never redeliver after 72h; keeping 7 days gives comfortable margin.
_RETENTION_DAYS = 7


async def is_duplicate(pool, channel: str, delivery_id: str) -> bool:
    """Return True if this delivery_id has already been processed for the channel."""
    row = await pool.fetchrow(
        """SELECT 1 FROM webhook_deliveries
           WHERE channel = $1 AND delivery_id = $2""",
        channel, delivery_id,
    )
    if row:
        _log.info("dedup: duplicate delivery — channel=%s delivery_id=%s", channel, delivery_id)
    return bool(row)


async def mark_processed(pool, channel: str, delivery_id: str) -> None:
    """Record that this delivery_id has been processed (idempotent insert)."""
    await pool.execute(
        """INSERT INTO webhook_deliveries (channel, delivery_id, created_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT DO NOTHING""",
        channel, delivery_id,
    )
    _log.debug("dedup: marked processed — channel=%s delivery_id=%s", channel, delivery_id)
