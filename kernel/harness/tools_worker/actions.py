"""Worker-side implementation for register_action_template.

Emits `action.template_discovered` into `ledger_events` chaining back
to the discovering agent and parent goal.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agentos_ledger_client.schema import ACTION_TEMPLATE_DISCOVERED
from kernel.harness.models.types import ToolResult
from kernel.ledger.actors import kernel_actor, principal_for
from kernel.ledger.emit import append

log = logging.getLogger(__name__)


async def register_action_template(
    conn,
    envelope,
    execution_id: int,
    tool_args: dict[str, Any],
    tool_call_id: str,
) -> ToolResult:
    """Register a discovered action template into the ledger with Merkle provenance."""
    template = tool_args.get("template")
    params = tool_args.get("params") or {}
    cohort_scope = str(tool_args.get("cohort_scope") or "").strip()
    description = str(tool_args.get("description") or "").strip()

    if not template or not cohort_scope:
        return ToolResult(
            tool_call_id=tool_call_id,
            output="",
            error="E_BAD_ARGS: register_action_template requires `template` and `cohort_scope`",
            exit_code=1,
        )

    # Resolve context: goal_id, story_id, actor
    ctx = await principal_for(conn, execution_id)
    goal_id = ctx.get("goal_id") if ctx else ""
    story_id = ctx.get("story_id") if ctx else ""
    kind = ctx.get("kind") if ctx else "story"
    domain = ctx.get("domain") if ctx else "coding"

    actor_id, actor_type = await kernel_actor(conn, "executor", kind, domain)
    on_behalf_of = ctx.get("human_actor_id") if ctx else None

    payload = {
        "execution_id": execution_id,
        "story_id": story_id,
        "goal_id": goal_id,
        "template": template,
        "params": params,
        "cohort_scope": cohort_scope,
        "description": description,
        "actor_uid": ctx.get("human_uid") if ctx else None,
    }

    event_id = await append(
        conn,
        event_type=ACTION_TEMPLATE_DISCOVERED,
        actor_id=actor_id,
        actor_type=actor_type,
        payload=payload,
        on_behalf_of=on_behalf_of,
        goal_id=goal_id,
        instance=f"exec:{execution_id}",
    )

    out = {
        "ok": True,
        "event_id": event_id,
        "template": template,
        "cohort_scope": cohort_scope,
        "goal_id": goal_id,
        "story_id": story_id,
    }
    log.info(
        "register_action_template: execution=%s goal=%s template=%s event_id=%s",
        execution_id,
        goal_id,
        template,
        event_id,
    )
    return ToolResult(
        tool_call_id=tool_call_id,
        output=json.dumps(out),
        exit_code=0,
    )
