"""DEPRECATED — superseded by platform_channel_bindings in the generic channel system.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Slack thread ↔ control panel thread mapping CRUD."""
import logging
from typing import Optional

from app.database import get_pool

_log = logging.getLogger(__name__)


async def get_mapping_by_slack_thread(
    channel_id: str, thread_ts: str,
) -> Optional[dict]:
    """Look up which control panel thread a Slack thread maps to."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_thread_map
         WHERE slack_channel_id = $1 AND slack_thread_ts = $2
    """, channel_id, thread_ts)
    return dict(row) if row else None


async def get_mapping_by_thread_id(thread_id: str) -> Optional[dict]:
    """Look up the Slack thread for a control panel thread (for outbound relay)."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_thread_map WHERE thread_id = $1
    """, thread_id)
    return dict(row) if row else None


async def create_mapping(
    slack_channel_id: str,
    slack_thread_ts: str,
    goal_id: str,
    thread_id: str = "",
    story_id: str = "",
    agent_id: str = "",
    thread_type: str = "go",
    user_uid: str = "",
) -> dict:
    pool = get_pool()
    row = await pool.fetchrow("""
        INSERT INTO slack_thread_map
            (slack_channel_id, slack_thread_ts, goal_id, thread_id,
             story_id, agent_id, thread_type, user_uid)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (slack_channel_id, slack_thread_ts) DO UPDATE SET
            thread_id = COALESCE(NULLIF($4, ''), slack_thread_map.thread_id),
            story_id = COALESCE(NULLIF($5, ''), slack_thread_map.story_id),
            agent_id = COALESCE(NULLIF($6, ''), slack_thread_map.agent_id)
        RETURNING *
    """, slack_channel_id, slack_thread_ts, goal_id, thread_id,
        story_id, agent_id, thread_type, user_uid)
    _log.info(
        "Mapped Slack thread %s/%s → goal=%s thread=%s story=%s type=%s",
        slack_channel_id, slack_thread_ts[:12], goal_id,
        thread_id[:12] if thread_id else "-",
        story_id[:8] if story_id else "-", thread_type,
    )
    return dict(row)


async def list_mappings_for_goal(goal_id: str) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT * FROM slack_thread_map
         WHERE goal_id = $1
         ORDER BY created_at DESC
    """, goal_id)
    return [dict(r) for r in rows]


# ── Slack user ↔ control panel user mapping ──────────────────────────────────

async def get_user_uid(slack_user_id: str, workspace_id: str) -> Optional[str]:
    """Resolve a Slack user to a control panel uid."""
    pool = get_pool()
    return await pool.fetchval("""
        SELECT user_uid FROM slack_user_map
         WHERE slack_user_id = $1 AND workspace_id = $2
    """, slack_user_id, workspace_id)


async def map_slack_user(
    slack_user_id: str,
    workspace_id: str,
    user_uid: str,
    slack_display_name: str = "",
    slack_email: str = "",
) -> None:
    pool = get_pool()
    await pool.execute("""
        INSERT INTO slack_user_map
            (slack_user_id, workspace_id, user_uid, slack_display_name, slack_email)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (slack_user_id, workspace_id) DO UPDATE SET
            user_uid = $3, slack_display_name = $4, slack_email = $5
    """, slack_user_id, workspace_id, user_uid, slack_display_name, slack_email)
    _log.info(
        "Mapped Slack user %s (%s) → uid=%s", slack_user_id, slack_display_name, user_uid,
    )


async def auto_map_user_by_email(
    slack_user_id: str,
    workspace_id: str,
    slack_email: str,
    slack_display_name: str = "",
) -> Optional[str]:
    """Try to auto-map a Slack user by matching their email to our users table.
    Returns the user_uid if found, None if no match.
    """
    pool = get_pool()
    user_uid = await pool.fetchval(
        "SELECT uid FROM users WHERE email = $1", slack_email,
    )
    if user_uid:
        await map_slack_user(
            slack_user_id, workspace_id, user_uid,
            slack_display_name, slack_email,
        )
    return user_uid
