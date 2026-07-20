"""Agent decommissioning — soft-delete an agent and destroy its gateway workspace.

SHCAs go through `agents.archive-and-delete` so MEMORY.md / memory/ / sessions/
are uploaded to GCS before the gateway removes the local workspace. Every
other agent type (IA today, anything else later) is plain-deleted — their
state is intentionally not persisted to GCS. GO is handled by
slack/lifecycle.py and never reaches this function.
"""
import logging
import uuid

from app.openclaw import gateway_admin as _default_gateway
from app.ledger.journal import append_event
from app.ledger.schema import AGENT_DELETED

logger = logging.getLogger(__name__)

# Operational system actor — used by reconcilers that have no human actor in scope.
# The one canonical, registered system actor (auth.SYSTEM_ACTOR_ID is now the same
# …0001 value, unified in the ledger provenance work).
from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID


async def _write_agent_deleted_rows(conn, agent_id: str, *, require_active: bool) -> bool:
    """UPDATE agents status='deleted' + DELETE binding row. Must run inside an open transaction.

    require_active=True: only updates the row if current status='active'; returns whether
                         the row changed (False = already deleted or not found).
    require_active=False: unconditional update; always returns True.
    """
    if require_active:
        result = await conn.execute(
            "UPDATE agents SET status = 'deleted' WHERE id = $1 AND status = 'active'",
            agent_id,
        )
        updated = result == "UPDATE 1"
    else:
        await conn.execute("UPDATE agents SET status = 'deleted' WHERE id = $1", agent_id)
        updated = True
    await conn.execute("DELETE FROM agent_gateway_bindings WHERE agent_id = $1", agent_id)
    return updated


async def soft_delete_agent(pool, agent_id: str, actor_id=None) -> bool:
    """DB-only agent soft-delete for reconciler paths that have already handled
    gateway teardown externally (watchdog, goal-delete, Slack lifecycle).

    Wraps the three operations that must be atomic:
      1. AGENT_DELETED ledger event (event-first)
      2. UPDATE agents SET status='deleted'
      3. DELETE FROM agent_gateway_bindings

    Returns True if the agent was found and marked deleted, False if it was
    already deleted or not found (idempotent — safe to call multiple times).

    Uses _SYSTEM_ACTOR_ID when no actor_id is provided, consistent with other
    operational system events (memory flush, story transitions).
    """
    resolved_actor = uuid.UUID(str(actor_id)) if actor_id else _SYSTEM_ACTOR_ID
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=AGENT_DELETED,
                payload={"agent_id": agent_id, "source": "reconciler"},
                actor_id=resolved_actor,
                actor_type="system",
            )
            updated = await _write_agent_deleted_rows(conn, agent_id, require_active=True)
    if updated:
        logger.info("soft_delete_agent: marked %s deleted + removed binding", agent_id)
    else:
        logger.debug("soft_delete_agent: %s already deleted or not found — no-op", agent_id)
    return updated


async def decommission_agent(pool, agent_id: str, actor_id: str, *, gateway=None) -> None:
    """Soft-delete an agent. Archives memory + session transcripts to GCS
    via `agents.archive-and-delete` for SHCAs, then marks the row deleted.

    For SHCAs the gateway-side archive is REQUIRED — if the archive fails
    for any file, the gateway refuses to remove the local workspace and
    this function raises so the caller can retry. The DB soft-delete only
    happens after the gateway has confirmed both archive and removal
    succeeded.

    For non-SHCA agents the gateway delete is best-effort — failures are
    logged, not raised, and the DB soft-delete proceeds.

    Raises:
        ValueError: Agent not found, or SHCA archive/delete failed.
    """
    gw = gateway if gateway is not None else _default_gateway
    row = await pool.fetchrow(
        """
        SELECT a.gateway_agent_id, a.type, gam.goal_id
          FROM agents a
          LEFT JOIN goal_agent_memberships gam ON gam.agent_id = a.id
         WHERE a.id = $1 AND a.status = 'active'
         LIMIT 1
        """,
        agent_id,
    )
    if not row:
        raise ValueError(f"Agent '{agent_id}' not found")

    gw_id = row["gateway_agent_id"] or agent_id
    agent_type = (row["type"] or "").upper()
    goal_id = row["goal_id"]

    if agent_type != "SHCA":
        try:
            await gw.delete_agent(gw_id, delete_files=True)
            logger.info("Agent %s (type=%s) destroyed (no archive)", gw_id, agent_type or "?")
        except Exception as exc:
            logger.warning(
                "Gateway agents.delete failed for %s (type=%s): %s",
                gw_id, agent_type or "?", exc,
            )
    else:
        if not goal_id:
            raise ValueError(
                f"Agent '{agent_id}' (gw={gw_id}, type={agent_type}) has no goal "
                f"binding; refusing to archive-and-delete without goal_id",
            )
        try:
            result = await gw.archive_and_delete_agent(gw_id, goal_id)
            uploaded = result.get("uploaded") if isinstance(result, dict) else None
            bucket = result.get("bucket") if isinstance(result, dict) else "?"
            total_bytes = result.get("totalBytes") if isinstance(result, dict) else 0
            logger.info(
                "Agent %s (type=%s) archived (%d files, %s bytes) to gs://%s/%s/%s and destroyed",
                gw_id, agent_type, len(uploaded or []), total_bytes,
                bucket, goal_id, gw_id.lower(),
            )
        except Exception as exc:
            logger.error(
                "archive_and_delete_agent failed for %s (goal=%s): %s",
                gw_id, goal_id, exc,
            )
            raise  # do NOT mark the DB row deleted — caller can retry

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=AGENT_DELETED,
                payload={"agent_id": agent_id, "gateway_agent_id": gw_id},
                actor_id=actor_id,
            )
            await _write_agent_deleted_rows(conn, agent_id, require_active=False)

    logger.info("Agent %s decommissioned", agent_id)
