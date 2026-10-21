"""Local Execution Mode — mint the story-scoped human-local token (M1).

The extension authenticates the org user (Firebase) and calls
/internal/local/story-token. We verify the caller owns the story
(story.assigned_to) and mint a short-lived human-local JWT scoped to that
(goal, story), carrying the user's SHCA instance so events attribute to the
assistant on-behalf-of the human. See LOCAL_EXECUTION_MODE_PLAN.md §6 / §14.1.
"""
from __future__ import annotations

from typing import Optional

from app.database import get_pool
from app.internal_auth import mint_human_local_token

STORY_TOKEN_TTL_SECONDS = 3600


async def _resolve_user_shca_instance(goal_id: str, uid: str) -> Optional[tuple[str, str]]:
    """Return (gateway_agent_id, agent_instances.id) for the user's active SHCA
    in this goal, or None. The instance id becomes the token's aiid so obo
    attribution resolves to owner_actor_id (the human)."""
    row = await get_pool().fetchrow(
        """SELECT ai.gateway_agent_id, ai.id::text AS aiid
             FROM agent_instances ai
             JOIN agents a ON a.id = ai.template_id
             JOIN users u ON u.actor_id = ai.owner_actor_id
            WHERE ai.goal_id = $1 AND u.uid = $2
              AND a.type = 'SHCA' AND ai.status = 'active'
            ORDER BY ai.created_at DESC
            LIMIT 1""",
        goal_id, uid,
    )
    return (row["gateway_agent_id"], row["aiid"]) if row else None


async def mint_story_token(uid: str, story_id: str) -> dict:
    """Verify the caller owns the story, then mint the human-local token.

    Returns {ok, token, expires_in} or {ok:False, reason} — reasons:
    story_not_found / not_assignee / no_assistant_instance.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT goal_id, assigned_to FROM stories WHERE id = $1", story_id,
    )
    if not story:
        return {"ok": False, "reason": "story_not_found"}
    assigned = (story["assigned_to"] or "").strip()
    if not assigned or assigned != uid:
        return {"ok": False, "reason": "not_assignee"}
    # Auto-provision the local SHCA identity + story thread if this is the first
    # local touch of the (goal, user) — a locally-executed story has no gateway
    # import/chat flow to create them (harness model: no live gateway agent).
    from app.services.local_assistant import ensure_local_assistant_instance  # noqa: PLC0415
    inst = await ensure_local_assistant_instance(story["goal_id"], uid, story_id)
    if not inst:
        return {"ok": False, "reason": "no_assistant_instance"}
    gateway_agent_id, aiid = inst
    token = mint_human_local_token(
        agent_id=gateway_agent_id,
        goal_id=story["goal_id"],
        story_id=story_id,
        agent_instance_id=aiid,
        ttl_seconds=STORY_TOKEN_TTL_SECONDS,
    )
    return {"ok": True, "token": token, "expires_in": STORY_TOKEN_TTL_SECONDS}
