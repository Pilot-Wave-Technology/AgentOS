"""
Notification helpers.

All writes go through push_notification() which uses a PostgreSQL transaction to:
  1. Insert the notification row
  2. Atomically increment users.unread_notifications

This means the badge counter is always consistent under concurrent writes.
"""
import uuid

from app.database import get_pool


async def push_notification(
    uid: str,
    *,
    type: str,
    title: str,
    body: str = "",
    goal_id: str = "",
    agent_id: str = "",
    thread_id: str = "",
    task_id: str = "",
    link: str = "",
) -> str:
    """
    Write one notification row and atomically increment the user's unread counter.
    Returns the notification ID.
    """
    pool = get_pool()
    notif_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO notifications
                       (id, uid, type, title, body, goal_id, agent_id, thread_id, task_id, link)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                notif_id, uid, type, title, body, goal_id, agent_id, thread_id, task_id, link,
            )
            await conn.execute(
                "UPDATE users SET unread_notifications = unread_notifications + 1 WHERE uid = $1",
                uid,
            )

    return notif_id


async def mark_read(uid: str, notif_id: str):
    """Mark one notification as read and decrement the counter (floor 0)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Only decrement if transitioning from unread → read
            result = await conn.execute(
                "UPDATE notifications SET read = TRUE WHERE id = $1 AND uid = $2 AND read = FALSE",
                notif_id, uid,
            )
            if result == "UPDATE 1":
                await conn.execute(
                    "UPDATE users SET unread_notifications = GREATEST(unread_notifications - 1, 0) WHERE uid = $1",
                    uid,
                )


async def mark_all_read(uid: str):
    """Mark every unread notification as read and zero the counter."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE notifications SET read = TRUE WHERE uid = $1 AND read = FALSE",
                uid,
            )
            await conn.execute(
                "UPDATE users SET unread_notifications = 0 WHERE uid = $1",
                uid,
            )
