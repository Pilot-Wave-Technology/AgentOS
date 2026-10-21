"""Local Execution Mode — conversation reporter intake (M3).

A local story's chat happens in the user's editor, not on the gateway. To keep
memory/continuity intact (§7.4, §14), the extension mirrors each turn — the human
message and the assistant/tool reply — into the story's persistent thread so it
lands in `messages` exactly like a gateway story. These rows are RECORDED for
continuity; they must NOT wake the gateway SHCA (the assistant is running locally,
so a gateway turn would double-execute). Mirrors `post_system_card_to_thread`'s
insert + fan-out, minus the agent forward.
"""
from __future__ import annotations

import json
import uuid

from app.database import get_pool

_ROLES = {"human", "agent"}
_DEFAULT_SENDER = {"human": "local-user", "agent": "local-assistant"}


async def _resolve_sender(pool, story_id: str, role: str) -> str:
    """Match the UI's messages.sender convention: a human turn's sender is the user's
    uid; an agent turn's sender is the assistant's agent_id. Falls back to the local-*
    placeholder only if the story/assistant can't be resolved."""
    uid = await pool.fetchval("SELECT assigned_to FROM stories WHERE id = $1", story_id) or ""
    if role == "human":
        return uid or _DEFAULT_SENDER["human"]
    if uid:
        from app.services.agent_registry.assistant_queries import (  # noqa: PLC0415
            get_user_assistant_agent_id_by_uid,
        )
        agent_id = await get_user_assistant_agent_id_by_uid(pool, uid)
        if agent_id:
            return agent_id
    return _DEFAULT_SENDER["agent"]


async def recent_local_messages(story_id: str, limit: int = 12) -> dict:
    """The last `limit` conversation turns on the story's thread, oldest-first — used to
    seed a REBUILT local session (its on-disk transcript was lost) so it recovers the
    dialogue, not just the work state. Only human/agent turns with text."""
    pool = get_pool()
    thread_id = await pool.fetchval("SELECT active_thread_id FROM stories WHERE id = $1", story_id)
    if not thread_id:
        return {"ok": True, "messages": []}
    rows = await pool.fetch(
        "SELECT role, content FROM messages "
        "WHERE thread_id = $1 AND role IN ('human', 'agent') AND coalesce(content, '') <> '' "
        "ORDER BY created_at DESC LIMIT $2",
        thread_id, limit,
    )
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return {"ok": True, "messages": msgs}


async def report_message(
    story_id: str, role: str, content: str, sender: str = "", card_data: dict | None = None,
) -> dict:
    """Append a local conversation turn to the story's active thread (record-only)."""
    if role not in _ROLES:
        return {"ok": False, "reason": "bad_role"}
    if not (content or card_data):
        return {"ok": False, "reason": "empty"}
    pool = get_pool()
    thread_id = await pool.fetchval(
        "SELECT active_thread_id FROM stories WHERE id = $1", story_id,
    )
    if not thread_id:
        return {"ok": False, "reason": "no_active_thread"}
    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count, card_data)
           VALUES ($1, $2, $3, $4, $5, 0, $6::jsonb)""",
        msg_id, thread_id, sender or await _resolve_sender(pool, story_id, role), role,
        content or "", json.dumps(card_data) if card_data else None,
    )
    try:
        # Durable cross-instance fan-out so open browser tabs + Slack/Notion mirrors
        # light up — the same path a gateway story uses. NO gateway wake (record-only).
        from app.chat_module.repository import notify_new_message  # noqa: PLC0415
        await notify_new_message(thread_id, msg_id)
    except Exception:
        pass
    return {"ok": True, "message_id": msg_id}


async def latest_card(pool, story_id: str, kind: str):
    """The newest card of `kind` on this story's conversation, or None.

    THERE IS NO `messages.story_id`. Two call sites queried
    `FROM messages WHERE story_id = $1` and both raised UndefinedColumn at
    runtime -- `approve_local_plan`, which would have 500'd the first time
    anybody approved a plan, and the parked-plan read in `local_state`, which
    500'd `get-state` on every single turn. The column has never existed; a
    message belongs to a THREAD, and the thread belongs to the story.

    BOTH WAYS ROUND, because both are real: `threads.story_id` is the
    per-story thread the current model creates, and `stories.active_thread_id`
    is what `report_message` actually writes through. A story bound before the
    per-story model, or one whose thread was rebound, has one and not the
    other.
    """
    return await pool.fetchval(
        """
        SELECT m.card_data
          FROM messages m
          JOIN threads t ON t.id = m.thread_id
          LEFT JOIN stories s ON s.id = $1
         WHERE (t.story_id = $1 OR t.id = s.active_thread_id)
           AND m.card_data->>'kind' = $2
         ORDER BY m.created_at DESC, m.id DESC
         LIMIT 1
        """, story_id, kind)


async def latest_work_plan_card(pool, story_id: str):
    """The newest plan card that is NOT a catch-up, or None.

    WHAT THE STORY IS ACTUALLY FOR. A catch-up plan carries the story's
    own checks forward, because approving a plan REPLACES the contract
    and an integration plan that carried none would narrow the story to
    the suite alone. But it read the LATEST plan card, and after one
    catch-up that IS a catch-up -- so the next offer carried the
    previous offer's generated mount checks instead of the story's.

    That is not merely redundant, it is how a bad mount became
    permanent. On 06295e15 a catch-up was written while the mount for
    two lanes of one repository collapsed to one directory. The code
    was fixed and the plan replaced -- and the replacement carried the
    collapsed check forward from the card it was replacing, so the new
    plan failed in exactly the old way. Damage that propagates through
    its own repair.

    The generated marker and suite checks need no carrying: they are
    rebuilt per mount on every offer, from the clones the story has
    now. Only the story's OWN checks have to survive, and those live on
    a work plan's card.
    """
    return await pool.fetchval(
        """
        SELECT m.card_data
          FROM messages m
          JOIN threads t ON t.id = m.thread_id
          LEFT JOIN stories s ON s.id = $1
         WHERE (t.story_id = $1 OR t.id = s.active_thread_id)
           AND m.card_data->>'kind' = 'plan_review'
           AND COALESCE(m.card_data->>'plan_kind', 'work') <> 'integration'
         ORDER BY m.created_at DESC, m.id DESC
         LIMIT 1
        """, story_id)
