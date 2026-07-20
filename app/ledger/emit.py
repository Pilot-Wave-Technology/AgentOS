"""Fire-and-forget event emitter for use in routers.

emit_event() never raises — if the ledger write fails (e.g., PG is down),
it logs a warning and returns None.
"""
import logging
import uuid as _uuid
from typing import Any, Optional

from app.database import get_pool
from app.ledger.journal import append_event

log = logging.getLogger(__name__)


async def emit_event(
    event_type: str,
    payload: dict[str, Any],
    actor_id: _uuid.UUID,
    actor_type: str = "human",
    on_behalf_of: Optional[_uuid.UUID] = None,
    event_version: int = 1,
    correlation_id: Optional[str] = None,
    causation_ids: Optional[list[str]] = None,
    context_task_id: str = "",
    context_goal_id: str = "",
    context_team_id: str = "",
    context_conversation_id: str = "",
    classification: str = "normal",
    extensions: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[dict]:
    """Best-effort ledger append.  Never raises."""
    pool = get_pool()
    if idempotency_key is None:
        idempotency_key = str(_uuid.uuid4())
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await append_event(
                    conn,
                    event_type=event_type,
                    payload=payload,
                    actor_id=actor_id,
                    actor_type=actor_type,
                    on_behalf_of=on_behalf_of,
                    event_version=event_version,
                    correlation_id=correlation_id,
                    causation_ids=causation_ids,
                    context_task_id=context_task_id,
                    context_goal_id=context_goal_id,
                    context_team_id=context_team_id,
                    context_conversation_id=context_conversation_id,
                    classification=classification,
                    extensions=extensions,
                    idempotency_key=idempotency_key,
                )
    except Exception:
        log.exception("Ledger emit failed: %s", event_type)
        return None
