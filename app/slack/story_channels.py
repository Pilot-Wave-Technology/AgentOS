"""DEPRECATED — superseded by platform_channel_bindings in the generic channel system.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

CRUD for slack_story_channels — one row per story channel (active or archived)."""
import logging
from typing import Optional

from app.database import get_pool

_log = logging.getLogger(__name__)


async def get_story_channel(goal_id: str, story_id: str) -> Optional[dict]:
    """Lookup by (goal_id, story_id). Returns row even if archived."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_story_channels
         WHERE goal_id = $1 AND story_id = $2
    """, goal_id, story_id)
    return dict(row) if row else None


async def get_by_channel_id(slack_channel_id: str) -> Optional[dict]:
    """Lookup by Slack channel ID. Returns row even if archived."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_story_channels
         WHERE slack_channel_id = $1
    """, slack_channel_id)
    return dict(row) if row else None


async def get_by_thread_id(thread_id: str) -> Optional[dict]:
    """Lookup by control-panel thread_id (for outbound relay)."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_story_channels
         WHERE thread_id = $1 AND archived_at IS NULL
    """, thread_id)
    return dict(row) if row else None


async def create_story_channel(
    goal_id: str,
    story_id: str,
    slack_channel_id: str,
    slack_channel_name: str,
    user_uid: str,
    thread_id: str,
    agent_id: str,
) -> dict:
    pool = get_pool()
    row = await pool.fetchrow("""
        INSERT INTO slack_story_channels
            (goal_id, story_id, slack_channel_id, slack_channel_name,
             user_uid, thread_id, agent_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (goal_id, story_id) DO UPDATE SET
            slack_channel_id = $3,
            slack_channel_name = $4,
            user_uid = $5,
            thread_id = $6,
            agent_id = $7,
            archived_at = NULL
        RETURNING *
    """, goal_id, story_id, slack_channel_id, slack_channel_name,
        user_uid, thread_id, agent_id)
    _log.info(
        "Created story channel: goal=%s story=%s channel=#%s uid=%s",
        goal_id, story_id, slack_channel_name, user_uid,
    )
    return dict(row)


async def mark_archived(goal_id: str, story_id: str) -> None:
    pool = get_pool()
    await pool.execute("""
        UPDATE slack_story_channels
           SET archived_at = now()
         WHERE goal_id = $1 AND story_id = $2
    """, goal_id, story_id)
    _log.info("Marked story channel archived: goal=%s story=%s", goal_id, story_id)


async def list_active_for_goal(goal_id: str) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT * FROM slack_story_channels
         WHERE goal_id = $1 AND archived_at IS NULL
         ORDER BY created_at DESC
    """, goal_id)
    return [dict(r) for r in rows]
