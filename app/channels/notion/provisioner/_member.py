"""Member page provisioning and lazy SHCA binding.

Each goal member gets a dedicated page under the goal page as their personal
SHCA chat surface. Comments on that page route to their SHCA thread.

Pending bindings (member page created but SHCA thread not yet started) are
represented as platform_channel_bindings rows with thread_id IS NULL.  When an
SHCA thread is first created, bind_pending_shca_subpage updates those rows in
place.  This pattern is generic — any platform can represent a "page/channel
exists but no thread yet" state the same way, with no extra tables.
"""
import logging

from ._helpers import _shca_for_member

_log = logging.getLogger(__name__)


async def _do_provision_member_subpage(
    goal_id: str,
    uid: str,
    integration_id: str,
    access_token: str,
    pool,
) -> None:
    """Create a member page under the goal page and bind it to the user's SHCA thread.

    If the user's SHCA thread already exists, inserts a fully-wired binding
    (thread_id + agent_id set).  Otherwise inserts a pending binding
    (thread_id IS NULL) that bind_pending_shca_subpage will complete when the
    SHCA is later created.
    """
    goal_page_id = await pool.fetchval(
        """SELECT external_channel_id FROM platform_channel_bindings
           WHERE goal_id = $1 AND integration_id = $2
             AND actor_id IS NULL AND archived_at IS NULL
           LIMIT 1""",
        goal_id, integration_id,
    )
    if not goal_page_id:
        _log.debug("_do_provision_member_subpage: no goal page binding goal=%s", goal_id)
        return

    user_row = await pool.fetchrow(
        "SELECT display_name, actor_id FROM users WHERE uid = $1", uid,
    )
    if not user_row or not user_row["actor_id"]:
        _log.debug("_do_provision_member_subpage: no actor_id for uid=%s", uid)
        return

    display_name = user_row["display_name"] or uid
    actor_id = user_row["actor_id"]

    try:
        from app.channels.notion.pages import create_member_subpage
        page_id = await create_member_subpage(goal_id, goal_page_id, uid, display_name, access_token)
    except Exception as exc:
        _log.warning("_do_provision_member_subpage: page creation failed uid=%s: %s", uid, exc)
        return

    shca = await _shca_for_member(pool, goal_id, actor_id)
    if shca:
        # SHCA already exists — insert a fully-wired binding immediately.
        await pool.execute(
            """INSERT INTO platform_channel_bindings
                   (goal_id, integration_id, actor_id, external_channel_id,
                    thread_id, agent_id, resource_kind)
               VALUES ($1, $2, $3, $4, $5, $6, 'comment')
               ON CONFLICT (integration_id, external_channel_id) DO UPDATE
                   SET thread_id   = EXCLUDED.thread_id,
                       agent_id    = EXCLUDED.agent_id,
                       archived_at = NULL""",
            goal_id, integration_id, actor_id, page_id,
            shca["thread_id"], shca["agent_id"],
        )
        _log.info(
            "Notion: member page bound goal=%s uid=%s page=%s thread=%s",
            goal_id, uid, page_id, str(shca["thread_id"])[:12],
        )
        return

    # SHCA not yet created — insert a pending binding (thread_id IS NULL).
    # bind_pending_shca_subpage will fill in thread_id + agent_id when the
    # SHCA session starts.  ON CONFLICT DO NOTHING so we don't overwrite an
    # already-active binding if provisioning runs more than once.
    await pool.execute(
        """INSERT INTO platform_channel_bindings
               (goal_id, integration_id, actor_id, external_channel_id,
                thread_id, agent_id, resource_kind)
           VALUES ($1, $2, $3, $4, NULL, NULL, 'comment')
           ON CONFLICT (integration_id, external_channel_id) DO NOTHING""",
        goal_id, integration_id, actor_id, page_id,
    )
    _log.info(
        "Notion: member page binding deferred (no SHCA yet) goal=%s uid=%s page=%s",
        goal_id, uid, page_id,
    )


async def bind_pending_shca_subpage(
    pool,
    goal_id: str,
    uid: str,
    agent_id: str,
    thread_id: str,
) -> None:
    """Claim pending member page bindings for a newly-created SHCA thread.

    Called when an SHCA session is first created.  Finds any platform_channel_bindings
    rows for this (goal, user) with thread_id IS NULL (pending state) and updates
    them with the new thread_id and agent_id.
    """
    actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", uid)
    if not actor_id:
        return

    rows = await pool.fetch(
        """SELECT pcb.id, pcb.external_channel_id
             FROM platform_channel_bindings pcb
             JOIN platform_integrations pi ON pi.id = pcb.integration_id
            WHERE pcb.goal_id = $1
              AND pcb.actor_id = $2
              AND pcb.thread_id IS NULL
              AND pcb.archived_at IS NULL
              AND pi.platform IN ('notion', 'notion_bot')
              AND pi.status = 'active'""",
        goal_id, actor_id,
    )

    for row in rows:
        await pool.execute(
            """UPDATE platform_channel_bindings
                  SET thread_id  = $1,
                      agent_id   = $2,
                      updated_at = now()
                WHERE id = $3
                  AND thread_id IS NULL""",
            thread_id, agent_id, row["id"],
        )
        _log.info(
            "Notion lazy member binding: goal=%s uid=%s page=%s thread=%s",
            goal_id, uid, row["external_channel_id"], thread_id[:12] if thread_id else "",
        )
