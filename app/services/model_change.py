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
from app.services.model_registry.dropdowns import is_model_registered_v2

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

    # 1. Validate the requested model. Accept ids present in the v2
    # Model Registry as well as the legacy `SUPPORTED_MODELS` allowlist
    # so a newly-registered DB model works end-to-end (story 5dafee43
    # AC-4). The DB check is skipped implicitly on pool exceptions —
    # `is_model_registered_v2` logs at WARNING and returns False,
    # leaving `is_supported` as the last line of defence.
    if (
        not is_supported(new_model)
        and not await is_model_registered_v2(pool, new_model)
    ):
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

    # 6. Reset every live session for this agent — both this process's
    # local _AgentSession cache AND the gateway's own SessionEntry cache
    # (agents.update above only rewrote the gateway's persisted config,
    # neither cache re-reads it on its own). See gateway_sessions.py.
    try:
        from app.services.gateway_sessions import reset_agent_sessions
        reset_count = await reset_agent_sessions(agent_id)
        _log.info(
            "Gateway session reset after model change: agent=%s reset_count=%d",
            agent_id, reset_count,
        )
    except Exception as exc:
        _log.warning(
            "Gateway session reset failed for agent=%s after model change: %s",
            agent_id, exc,
        )

    _log.info(
        "Model change OK: agent=%s %s -> %s (force=%s) by uid=%s",
        agent_id, old_model, new_model, force, actor_uid or "-",
    )
    return ModelChangeResult(
        ok=True, old_model=old_model, new_model=new_model,
    )
