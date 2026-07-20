"""Goal-level bulk teardown — destroy all gateway agents owned by a deleted goal.

Called as a background asyncio.Task by goals.py:delete_goal immediately after the
goal row is marked deleted. Also re-exported as the body of
slack/lifecycle.py:destroy_goal_agent_instances (kept as a thin wrapper there for
backward compatibility).

All four agent types are handled here:
  - SHCA / IA / RA  → rows in agent_instances joined to the goal
  - GO              → not in agent_instances; its gateway_agent_id lives on goals.go_agent_id

The goal row MUST already be status='deleted' before this is called so that concurrent
activity checks see the goal as inactive while teardown runs.
"""
import asyncio
import logging

from app.openclaw import gateway_admin as _default_gateway
from .decommission import soft_delete_agent

logger = logging.getLogger(__name__)


async def teardown_goal_agents(pool, goal_id: str, *, gateway=None) -> dict:
    """Destroy every gateway agent owned by a deleted goal and clean up DB rows.

    Steps:
      1. Query agent_instances for this goal (SHCA/IA/RA). For each:
         - SHCA: gateway archive-and-delete (uploads memory to GCS first)
         - Others: plain gateway delete (ephemeral, no archive needed)
         - Mark the agent_instances row destroyed, evict in-memory session.
      2. GO agent (from goals.go_agent_id): evict session, archive-and-delete,
         then soft-delete the agents-table row.
      3. DB cleanup: delete agent_contexts for the goal, then soft-delete any SHCA
         agent whose agents-table row has no remaining active-goal bindings.

    Idempotent: already-destroyed instances and already-deleted rows are skipped
    silently. Gateway failures are logged but never raised — flakiness must not
    block the goal-delete redirect.

    Returns {"destroyed": int, "errors": list[str]}.
    """
    from app.openclaw import session_manager
    gw = gateway if gateway is not None else _default_gateway
    summary: dict = {"destroyed": 0, "errors": []}

    # ── 1. SHCA / IA / RA from agent_instances ───────────────────────────────
    # Join agents.type to decide archive-vs-plain-delete per instance.
    rows = await pool.fetch(
        """
        SELECT ai.id, ai.gateway_agent_id, ai.template_id, ai.thread_id, ai.goal_id,
               a.type AS agent_type
          FROM agent_instances ai
          JOIN agents a ON a.id = ai.template_id
         WHERE ai.goal_id = $1 AND ai.status = 'active'
           AND ai.gateway_agent_id <> ''
        """,
        goal_id,
    )

    for inst in rows:
        gw_id = inst["gateway_agent_id"]
        agent_type = (inst["agent_type"] or "").upper()
        # Retry the gateway delete — a transient timeout (e.g. a busy gateway) must
        # not strand the agent in the OpenClaw config forever, since teardown has no
        # later pass. "not found" means it's already gone → treat as success.
        for attempt in range(3):
            try:
                if agent_type == "SHCA":
                    await gw.archive_and_delete_agent(gw_id, goal_id)
                    logger.info("teardown: archived+destroyed SHCA %s (goal=%s)", gw_id, goal_id)
                else:
                    await gw.delete_agent(gw_id, delete_files=True)
                    logger.info("teardown: destroyed %s %s (goal=%s)", agent_type or "?", gw_id, goal_id)
                summary["destroyed"] += 1
                break
            except Exception as exc:
                if "not found" in str(exc).lower():
                    break
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                logger.warning(
                    "teardown: gateway delete failed for %s (type=%s) after retries: %s",
                    gw_id, agent_type, exc,
                )
                summary["errors"].append(f"{gw_id}:{exc}")

        try:
            # Update ALL active rows sharing this gateway_agent_id, not just the one we
            # iterated. Duplicate provisioning can create multiple instance rows for the
            # same gateway agent; updating only inst["id"] leaves the others orphaned
            # as status='active' even though the gateway agent is already gone.
            updated = await pool.execute(
                "UPDATE agent_instances SET status = 'destroyed', destroyed_at = now()"
                " WHERE gateway_agent_id = $1 AND status = 'active'",
                gw_id,
            )
            logger.debug("teardown: marked instances destroyed for gateway_agent_id=%s (%s)", gw_id, updated)
        except Exception as exc:
            logger.warning("teardown: failed to mark instances destroyed for %s: %s", gw_id, exc)

        try:
            session_manager.evict_thread(inst["goal_id"], inst["template_id"], inst["thread_id"])
        except Exception:
            pass

        # Clear operator workspace state for assistant deletions so the story
        # drops out of token-refresh and provisioning queries.
        if agent_type == "SHCA" and inst.get("thread_id"):
            try:
                from app.services.operator_provisioning import (
                    clear_operator_workspace_on_assistant_delete,
                )
                await clear_operator_workspace_on_assistant_delete(
                    pool, inst["goal_id"], inst["thread_id"],
                )
            except Exception:
                pass  # non-fatal

    # ── 2. GO agent ───────────────────────────────────────────────────────────
    # Not in agent_instances — fetched from goals.go_agent_id directly.
    go_row = await pool.fetchrow("SELECT go_agent_id FROM goals WHERE id = $1", goal_id)
    go_agent_id = (go_row["go_agent_id"] or "") if go_row else ""
    if go_agent_id:
        # Evict the in-memory session before archive so no new messages arrive
        # during the GCS upload window.
        try:
            sess = session_manager._sessions.pop((goal_id, go_agent_id), None)
            if sess:
                await sess.stop()
                logger.debug("teardown: evicted GO session %s (goal=%s)", go_agent_id, goal_id)
        except Exception:
            pass
        try:
            await gw.archive_and_delete_agent(go_agent_id, goal_id)
            logger.info("teardown: archived+destroyed GO %s (goal=%s)", go_agent_id, goal_id)
            summary["destroyed"] += 1
        except Exception as exc:
            logger.warning("teardown: GO archive-and-delete failed for %s: %s", go_agent_id, exc)
            summary["errors"].append(f"{go_agent_id}:{exc}")
        await soft_delete_agent(pool, go_agent_id)

    # ── 3. DB cleanup: agent_contexts + orphaned SHCA agents rows ─────────────
    # Belt-and-braces with goals.py synchronous block in older code paths.
    # All three operations are idempotent (DELETE/UPDATE with status check).
    try:
        shca_ids_in_goal = await pool.fetch(
            """SELECT DISTINCT ac.agent_id
                 FROM agent_contexts ac
                 JOIN agents a ON a.id = ac.agent_id AND a.type = 'SHCA'
                WHERE ac.goal_id = $1""",
            goal_id,
        )
        await pool.execute("DELETE FROM agent_contexts WHERE goal_id = $1", goal_id)
        orphaned = 0
        for r in shca_ids_in_goal:
            sid = r["agent_id"]
            still_bound = await pool.fetchval(
                """SELECT 1
                     FROM agent_contexts ac
                     JOIN goals g ON g.id = ac.goal_id AND g.status = 'active'
                    WHERE ac.agent_id = $1
                    LIMIT 1""",
                sid,
            )
            if not still_bound:
                await soft_delete_agent(pool, sid)
                orphaned += 1
        if orphaned:
            logger.info(
                "teardown: marked %d orphaned SHCA(s) deleted for goal %s", orphaned, goal_id,
            )
    except Exception as exc:
        logger.warning("teardown: DB cleanup failed for goal %s: %s", goal_id, exc)

    logger.info(
        "teardown_goal_agents DONE goal=%s destroyed=%d errors=%d",
        goal_id, summary["destroyed"], len(summary["errors"]),
    )
    return summary
