"""Shared binding helpers — split, stale-repair, and lazy SHCA bind."""
import logging

_log = logging.getLogger(__name__)

# Internal gateway signals that should never be forwarded to external platforms.
# "NO" is the model's bare-shorthand for the NO_REPLY silent turn. Matched by exact
# `content.strip() in _INTERNAL_SIGNALS` (agent.py / system.py), so only a message that
# is exactly "NO" is filtered — a real "No." / "No, ..." answer is unaffected.
_INTERNAL_SIGNALS = {"NO_REPLY", "NO", "HEARTBEAT_OK"}

# Notion's rich_text content limit is 2000 UTF-16 code units (not Python codepoints).
# Emoji outside the BMP (e.g. 🎯) are 1 Python char but 2 UTF-16 units, so we must
# count UTF-16 units and chunk accordingly.
_NOTION_BLOCK_LIMIT = 2000


def _split_for_notion(text: str, limit: int = _NOTION_BLOCK_LIMIT) -> list[str]:
    """Split text into chunks where each chunk ≤ limit UTF-16 code units."""
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for ch in text:
        ch_units = 2 if ord(ch) > 0xFFFF else 1
        if units + ch_units > limit:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(ch)
        units += ch_units
    if current:
        chunks.append("".join(current))
    return chunks or [""]


async def _repair_stale_shca_binding(platform: str, goal_id: str, thread_id: str) -> dict | None:
    """Find and repair a stale SHCA binding whose thread_id no longer matches the current thread.

    When an SHCA is reaped and recreated its thread_id changes, but platform_channel_bindings
    still holds the old value. This looks up by (goal_id, actor_id) instead, updates the row
    to the current thread_id, and returns the repaired binding so the relay can proceed.
    """
    try:
        from app.database import get_pool
        pool = get_pool()

        agent_row = await pool.fetchrow(
            """SELECT a.owner_actor_id, a.id AS agent_id
               FROM threads t
               JOIN agents a ON a.id = t.agent_id
               WHERE t.id = $1""",
            thread_id,
        )
        if not agent_row:
            return None
        actor_id = agent_row["owner_actor_id"]
        agent_id = str(agent_row["agent_id"])

        binding_row = await pool.fetchrow(
            """SELECT pcb.integration_id, pcb.actor_id,
                      pcb.external_channel_id, pcb.resource_kind
               FROM platform_channel_bindings pcb
               JOIN platform_integrations pi ON pi.id = pcb.integration_id
               JOIN goal_platform_integrations gpi ON gpi.integration_id = pcb.integration_id
                                                  AND gpi.goal_id = pcb.goal_id
               WHERE pi.platform    = $1
                 AND pcb.goal_id    = $2
                 AND pcb.actor_id   = $3
                 AND pcb.archived_at IS NULL
                 AND pi.status      = 'active'
                 AND gpi.enabled    = true
               LIMIT 1""",
            platform, goal_id, actor_id,
        )
        if not binding_row:
            return None

        await pool.execute(
            """UPDATE platform_channel_bindings
               SET thread_id = $1, agent_id = $2
               WHERE integration_id      = $3
                 AND external_channel_id = $4
                 AND archived_at IS NULL""",
            thread_id, agent_id,
            binding_row["integration_id"], binding_row["external_channel_id"],
        )
        _log.info(
            "Notion relay: repaired stale binding goal=%s page=%s platform=%s",
            goal_id, binding_row["external_channel_id"], platform,
        )
        return {
            "goal_id": goal_id,
            "integration_id": binding_row["integration_id"],
            "actor_id": actor_id,
            "external_channel_id": binding_row["external_channel_id"],
            "resource_kind": binding_row["resource_kind"],
            "agent_id": agent_id,
        }
    except Exception as exc:
        _log.debug("Notion stale binding repair failed goal=%s thread=%s: %s", goal_id, thread_id[:12] if thread_id else "", exc)
        return None


async def _lazy_bind_shca_thread(goal_id: str, thread_id: str) -> None:
    """Claim a pending Notion subpage for an SHCA thread if one was stored at provision time.

    Fires only when relay_agent_message finds no binding, so the cost is one
    DB lookup per message until the binding is created, then never again.
    """
    try:
        from app.database import get_pool
        pool = get_pool()
        row = await pool.fetchrow(
            """SELECT a.id AS agent_id, u.uid
               FROM threads t
               JOIN agents a ON a.id = t.agent_id
               JOIN users u ON u.actor_id = a.owner_actor_id
               WHERE t.id = $1 AND a.type = 'SHCA'
               LIMIT 1""",
            thread_id,
        )
        if not row:
            return
        from app.channels.notion.provisioner import bind_pending_shca_subpage
        await bind_pending_shca_subpage(
            pool, goal_id, row["uid"], str(row["agent_id"]), thread_id,
        )
    except Exception as exc:
        _log.debug("Notion lazy SHCA bind failed goal=%s thread=%s: %s", goal_id, thread_id[:12] if thread_id else "", exc)
