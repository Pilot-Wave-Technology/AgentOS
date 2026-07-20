"""Slack user ↔ internal actor mapping using platform_user_map (generic channel table).

Does NOT use the legacy slack_user_map table. All user mappings go through
platform_user_map with platform='slack' so the generic resolve_actor path works.
"""
import logging
from typing import Optional

_log = logging.getLogger(__name__)


async def get_user_uid(slack_user_id: str, pool) -> Optional[str]:
    """Resolve a Slack user ID to a control-panel user UID.

    Returns the UID string, or None if not mapped (or mapping is pending_review).
    """
    actor_id = await pool.fetchval(
        """SELECT actor_id FROM platform_user_map
           WHERE platform = 'slack'
             AND external_user_id = $1
             AND mapping_status = 'confirmed'
           LIMIT 1""",
        slack_user_id,
    )
    if not actor_id:
        return None
    uid = await pool.fetchval("SELECT uid FROM users WHERE actor_id = $1", actor_id)
    return str(uid) if uid else None


async def get_actor_id_for_uid(user_uid: str, pool):
    """Return the actor_id UUID for a user UID, or None."""
    return await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)


async def get_slack_user_id_for_actor(actor_id, pool) -> Optional[str]:
    """Return the Slack user ID for an internal actor, or None."""
    row = await pool.fetchrow(
        """SELECT external_user_id FROM platform_user_map
           WHERE platform = 'slack'
             AND actor_id = $1
             AND mapping_status = 'confirmed'
           LIMIT 1""",
        actor_id,
    )
    return row["external_user_id"] if row else None


async def map_slack_user(
    slack_user_id: str,
    user_uid: str,
    slack_display_name: str = "",
    slack_email: str = "",
    pool=None,
) -> None:
    """Persist a Slack user ID → internal actor mapping in platform_user_map."""
    actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)
    if not actor_id:
        _log.warning("map_slack_user: no actor_id for uid=%s", user_uid)
        return
    await pool.execute(
        """INSERT INTO platform_user_map
               (platform, external_user_id, actor_id, display_name, email, mapping_status)
           VALUES ('slack', $1, $2, $3, $4, 'confirmed')
           ON CONFLICT (platform, external_user_id) DO UPDATE SET
               actor_id = $2, display_name = $3, email = $4, mapping_status = 'confirmed'""",
        slack_user_id, actor_id, slack_display_name, slack_email,
    )
    _log.info("Mapped Slack user %s → uid=%s actor=%s", slack_user_id, user_uid, actor_id)


async def auto_map_by_email(
    slack_user_id: str,
    email: str,
    display_name: str = "",
    pool=None,
) -> Optional[str]:
    """Try to map a Slack user to a control-panel UID via their email address.

    Returns the UID on success, None if no matching user exists.
    """
    # Slack's reported profile email may differ in case/whitespace from what the
    # user registered with — normalize both sides so a cosmetic mismatch doesn't
    # silently block mapping forever.
    uid = await pool.fetchval(
        "SELECT uid FROM users WHERE lower(trim(email)) = lower(trim($1))", email,
    )
    if not uid:
        return None
    await map_slack_user(slack_user_id, uid, display_name, email, pool=pool)
    return str(uid)