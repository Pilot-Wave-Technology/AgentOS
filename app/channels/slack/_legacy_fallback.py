"""Legacy Slack table fallback for the platform-channel migration window.

During the cutover period, goals created after the backfill migration but before
the Slack App URL flip (or before the next manual backfill run) will have their
Slack config only in the old tables:
  slack_workspaces / slack_config / slack_user_goal_channels / slack_user_map

This module reads from those tables when the new platform_* tables have no matching
row, then immediately backfills the result so that all subsequent reads and writes
go through the new system.

DELETE THIS FILE once the Slack cutover is fully verified in production and the
legacy tables are confirmed empty. Two call sites to remove at that point:
  app/channels/base.py: get_binding()   — remove the _legacy_fallback block
  app/channels/base.py: resolve_actor() — remove the _legacy_fallback block

See documentation/SLACK_CUTOVER.md for the full cutover procedure.
"""
import logging
import uuid
from typing import Optional

_log = logging.getLogger(__name__)

# Platform slug used for every INSERT into the new tables.
_PLATFORM = "slack"


async def get_binding(external_channel_id: str, pool) -> Optional[dict]:
    """Look up a Slack binding in the legacy slack_user_goal_channels table.

    Covers SHCA (per-user) bindings only — GO goal-channel bindings are
    inserted directly during provision_goal and are not in the legacy table.

    Returns the same dict shape as base.get_binding(), or None.
    """
    row = await pool.fetchrow(
        """
        SELECT
            ugc.goal_id,
            ugc.thread_id,
            ugc.agent_id,
            ugc.current_story_id,
            u.actor_id,
            sw.workspace_id,
            sw.bot_token
        FROM slack_user_goal_channels ugc
        JOIN users        u  ON u.uid          = ugc.user_uid
        JOIN slack_config sc ON sc.goal_id     = ugc.goal_id
        JOIN slack_workspaces sw ON sw.workspace_id = sc.workspace_id
        WHERE ugc.slack_channel_id = $1
          AND ugc.archived_at      IS NULL
          AND sc.enabled           = true
          AND u.actor_id           IS NOT NULL
        LIMIT 1
        """,
        external_channel_id,
    )
    if row is None:
        return None

    _log.info(
        "legacy_fallback.get_binding: hit legacy table for channel=%s goal=%s — will backfill",
        external_channel_id, row["goal_id"],
    )

    return {
        "goal_id": row["goal_id"],
        "integration_id": None,          # resolved during backfill
        "actor_id": row["actor_id"],
        "thread_id": row["thread_id"],
        "agent_id": row["agent_id"],
        "current_story_id": row["current_story_id"],
        "resource_kind": "channel",
        "email_domain_allowlist": [],
        # Carry workspace info for backfill — not part of the normal binding shape
        "_legacy_workspace_id": row["workspace_id"],
    }


async def resolve_actor(external_user_id: str, pool) -> Optional[uuid.UUID]:
    """Look up a Slack user in the legacy slack_user_map table.

    Returns the actor_id if found and the user has one, or None.
    """
    row = await pool.fetchrow(
        """
        SELECT u.actor_id, sum2.slack_display_name, sum2.slack_email
        FROM slack_user_map sum2
        JOIN users u ON u.uid = sum2.user_uid
        WHERE sum2.slack_user_id = $1
          AND u.actor_id IS NOT NULL
        LIMIT 1
        """,
        external_user_id,
    )
    if row is None:
        return None

    _log.info(
        "legacy_fallback.resolve_actor: hit legacy table for slack_user=%s actor=%s — will backfill",
        external_user_id, row["actor_id"],
    )
    return row["actor_id"]


async def backfill_binding(binding: dict, external_channel_id: str, pool) -> None:
    """Upsert a legacy binding into the new platform_* tables.

    After this runs, get_binding() in base.py will find the row in the new tables
    on all subsequent calls, and all writes (current_story_id rotation, archiving)
    will go to platform_channel_bindings naturally.

    This is a best-effort operation — failures are logged but not re-raised so the
    inbound message is still routed via the already-resolved binding dict.
    """
    try:
        workspace_id = binding.get("_legacy_workspace_id")
        if not workspace_id:
            _log.warning("legacy_fallback.backfill_binding: no workspace_id in binding, skipping")
            return

        # 1. Ensure platform_integrations row exists for this workspace.
        integration_id = await pool.fetchval(
            "SELECT id FROM platform_integrations WHERE platform = $1 AND workspace_id = $2",
            _PLATFORM, workspace_id,
        )
        if integration_id is None:
            # Workspace credentials not yet in new tables — run the full backfill migration
            # or wait for the next scheduled run. Skip for now so we don't partial-insert.
            _log.warning(
                "legacy_fallback.backfill_binding: platform_integrations row missing for "
                "workspace=%s — run backfill_slack_legacy_to_platform_channels migration",
                workspace_id,
            )
            return

        goal_id = binding["goal_id"]

        # 2. Ensure goal_platform_integrations row exists.
        gpi_id = await pool.fetchval(
            """
            SELECT id FROM goal_platform_integrations
            WHERE goal_id = $1 AND integration_id = $2
            """,
            goal_id, integration_id,
        )
        if gpi_id is None:
            # Goal not yet in new tables either — same situation as above.
            _log.warning(
                "legacy_fallback.backfill_binding: goal_platform_integrations row missing for "
                "goal=%s integration=%s — run backfill migration",
                goal_id, integration_id,
            )
            return

        # 3. Upsert the channel binding. ON CONFLICT updates everything except
        #    archived_at so we don't accidentally un-archive a deliberately archived channel.
        await pool.execute(
            """
            INSERT INTO platform_channel_bindings
                (goal_id, integration_id, actor_id, external_channel_id,
                 resource_kind, thread_id, agent_id, current_story_id)
            VALUES ($1, $2, $3, $4, 'channel', $5, $6, $7)
            ON CONFLICT (integration_id, external_channel_id) DO UPDATE
                SET thread_id        = EXCLUDED.thread_id,
                    agent_id         = EXCLUDED.agent_id,
                    current_story_id = EXCLUDED.current_story_id
            """,
            goal_id,
            integration_id,
            binding["actor_id"],
            external_channel_id,
            binding["thread_id"] or None,
            binding["agent_id"] or None,
            binding["current_story_id"],
        )

        _log.info(
            "legacy_fallback.backfill_binding: backfilled channel=%s goal=%s integration=%s",
            external_channel_id, goal_id, integration_id,
        )

    except Exception as exc:
        _log.warning(
            "legacy_fallback.backfill_binding: failed for channel=%s goal=%s: %s",
            external_channel_id, binding.get("goal_id"), exc,
        )


async def backfill_actor(
    external_user_id: str,
    actor_id: uuid.UUID,
    display_name: Optional[str],
    email: Optional[str],
    pool,
) -> None:
    """Upsert a legacy user mapping into platform_user_map.

    After this runs, resolve_actor() in base.py will find the row in the new table
    on cache-hit (step 1) and never reach the legacy fallback again.
    """
    try:
        await pool.execute(
            """
            INSERT INTO platform_user_map
                (platform, external_user_id, actor_id, display_name, email, mapping_status)
            VALUES ($1, $2, $3, $4, $5, 'confirmed')
            ON CONFLICT (platform, external_user_id) DO NOTHING
            """,
            _PLATFORM, external_user_id, actor_id, display_name, email,
        )
        _log.info(
            "legacy_fallback.backfill_actor: backfilled slack_user=%s actor=%s",
            external_user_id, actor_id,
        )
    except Exception as exc:
        _log.warning(
            "legacy_fallback.backfill_actor: failed for slack_user=%s actor=%s: %s",
            external_user_id, actor_id, exc,
        )
