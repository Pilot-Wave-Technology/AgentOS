"""Live model change for an agent (GO or SHCA).

Used by both Phase-3 (goal settings → GO model change) and Phase-4
(my-agents → SHCA model change) UI endpoints. Encapsulates:

  • whitelist validation
  • busy-gate via `chat_run_state`
  • DB write to `agents.model`
  • gateway-side `agents.update` call
  • in-memory session evict so the next message uses the new model
  • ledger event (AGENT_UPDATED) for audit

The chat_run_state gate is the safety rail per the design discussion:
swapping a model mid-inference would orphan tool calls. If any thread
under this agent has a chat_run_state row (idle is represented by the
ABSENCE of a row), the change is refused unless `force=True`.
"""
from __future__ import annotations

import logging
from typing import NamedTuple, Optional

from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import AGENT_UPDATED
from app.llm_models import is_supported, label_for

_log = logging.getLogger(__name__)


class ModelChangeResult(NamedTuple):
    ok:        bool
    error:     str = ""
    was_busy:  bool = False     # True iff we refused because of an active run
    old_model: str = ""
    new_model: str = ""


async def change_agent_model(
    *,
    agent_id: str,
    new_model: str,
    actor_id: Optional[str],
    actor_uid: str = "",
    force: bool = False,
) -> ModelChangeResult:
    """Change `agents.model` for `agent_id` to `new_model`.

    `actor_id` is the users.actor_id of the person making the change
    (used as the ledger event's actor). `actor_uid` is the Firebase uid
    for human-readable logging.

    `force=True` skips the chat_run_state gate — use only when the
    caller has confirmed the user wants to interrupt an in-flight turn.
    """
    pool = get_pool()

    # 1. Validate the requested model.
    if not is_supported(new_model):
        return ModelChangeResult(
            ok=False, error=f"Model '{new_model}' is not in the supported list",
        )

    # 2. Look up the agent.
    agent_row = await pool.fetchrow(
        "SELECT id, model, gateway_agent_id, type, scope FROM agents WHERE id = $1",
        agent_id,
    )
    if not agent_row:
        return ModelChangeResult(
            ok=False, error=f"Agent '{agent_id}' not found",
        )
    old_model = agent_row["model"] or ""
    gw_id = agent_row["gateway_agent_id"] or ""

    if old_model == new_model:
        return ModelChangeResult(
            ok=True, old_model=old_model, new_model=new_model,
        )

    # 3. Busy gate. chat_run_state has a row iff the thread has an
    # in-flight run — idle is represented by row absence, so EXISTS
    # is the right test. Look at every thread that points at this
    # agent (covers both SHCA per-conversation threads and the GO
    # goal thread) AND any thread tied to its agent_instances rows
    # (covers the per-conversation gateway instances we provision
    # for SHCA work).
    if not force:
        busy = await pool.fetchval("""
            SELECT EXISTS(
                SELECT 1
                  FROM chat_run_state crs
                  JOIN threads t ON t.id = crs.thread_id
                 WHERE t.agent_id = $1
            )
            OR EXISTS(
                SELECT 1
                  FROM chat_run_state crs
                  JOIN agent_instances ai ON ai.thread_id = crs.thread_id
                 WHERE ai.template_id = $1 AND ai.status = 'active'
            )
        """, agent_id)
        if busy:
            _log.info(
                "change_agent_model REFUSED (busy): agent=%s old=%s new=%s actor=%s",
                agent_id, old_model, new_model, actor_uid or "-",
            )
            return ModelChangeResult(
                ok=False, was_busy=True,
                error=(
                    f"Agent '{agent_id}' is currently working — wait a few "
                    "seconds and retry, or pass force=true to interrupt."
                ),
                old_model=old_model, new_model=new_model,
            )

    # 4. DB update + ledger event in a single transaction (event-first).
    # append_event requires a Connection (not a Pool) — it uses
    # pg_advisory_xact_lock which is transaction-scoped. Both writes
    # must be atomic so the audit trail and projection cannot diverge.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=AGENT_UPDATED,
                payload={
                    "agent_id":  agent_id,
                    "field":     "model",
                    "old_value": old_model,
                    "new_value": new_model,
                    "old_label": label_for(old_model),
                    "new_label": label_for(new_model),
                    "force":     force,
                    "actor_uid": actor_uid,
                },
                actor_id=actor_id,
            )
            await conn.execute(
                """UPDATE agents
                   SET model = $1, updated_at = now(),
                       config_version = config_version + 1,
                       gateway_synced_version = NULL, gateway_synced_at = NULL
                   WHERE id = $2""",
                new_model, agent_id,
            )

    # 5. Gateway-side update so the running gateway picks up the new
    # model on the next session load. Soft-fail: if the gateway is
    # down, the DB change still landed and the next session-creation
    # path will read the right model.
    if gw_id:
        try:
            from app.openclaw import gateway_admin
            await gateway_admin.call("agents.update", {
                "agentId": gw_id,
                "model":   new_model,
            })
            _log.info(
                "Gateway agents.update OK: gw_id=%s model %s -> %s",
                gw_id, old_model, new_model,
            )
        except Exception as exc:
            _log.warning(
                "Gateway agents.update failed for %s (%s -> %s): %s — "
                "DB updated, will take effect on next session load",
                gw_id, old_model, new_model, exc,
            )

    # 6. Evict any in-memory _AgentSession objects so the next message
    # rebuilds them with the new model. (The session caches model-
    # specific state at construction.)
    try:
        from app.openclaw import session_manager
        # Collect (goal_id, agent_id, thread_id) keys to evict.
        keys_to_evict = [
            key for key in list(session_manager._sessions.keys())
            if isinstance(key, tuple)
            and (
                # SHCA: key is (goal_id, template_agent_id, thread_id)
                (len(key) == 3 and key[1] == agent_id)
                # GO: key is (goal_id, agent_id) — 2-tuple
                or (len(key) == 2 and key[1] == agent_id)
            )
        ]
        for key in keys_to_evict:
            session = session_manager._sessions.pop(key, None)
            if session:
                try:
                    await session.stop()
                except Exception:
                    pass
        if keys_to_evict:
            _log.info(
                "Evicted %d in-memory session(s) for agent=%s after model change",
                len(keys_to_evict), agent_id,
            )
    except Exception as exc:
        _log.warning(
            "Session evict failed for agent=%s after model change: %s",
            agent_id, exc,
        )

    _log.info(
        "Model change OK: agent=%s %s -> %s (force=%s) by uid=%s",
        agent_id, old_model, new_model, force, actor_uid or "-",
    )
    return ModelChangeResult(
        ok=True, old_model=old_model, new_model=new_model,
    )
