"""Local Execution Mode — auto-provision the (goal, user) assistant identity.

A gateway story gets its SHCA instance from the import/chat flow; a
locally-checked-out story had nothing provisioning it, so the story-token mint
404'd with `no_assistant_instance`. This ensures a lightweight `agent_instances`
identity row exists — WITHOUT a live gateway agent (the harness runs the assistant
locally): it is the token's aiid + the memory key. Idempotent.

The story's THREAD is NOT handled here — the local checkout calls the canonical
`provision_assistant_thread_for_story`, which binds the story to the user's SINGLE
(goal, user) assistant thread (all the user's stories in a goal share one thread,
matching the web UI), so we deliberately do not create a per-story thread.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from app.database import get_pool
from app.services.agent_registry.assistant_queries import get_user_assistant_agent_id_by_uid
from app.services.local_auth import _resolve_user_shca_instance

_log = logging.getLogger(__name__)


async def _ensure_assistant_agent(pool, uid: str) -> Optional[str]:
    """The user's SHCA assistant agent id, auto-creating it if this user has never
    opened the web assistant (reuses the login-time provisioner)."""
    agent_id = await get_user_assistant_agent_id_by_uid(pool, uid)
    if agent_id:
        return agent_id
    user = await pool.fetchrow(
        "SELECT actor_id, display_name, email FROM users WHERE uid = $1", uid,
    )
    if not user:
        return None
    from app.assistant_provisioning import ensure_user_assistant  # noqa: PLC0415
    res = await ensure_user_assistant(
        uid, user["actor_id"], user["display_name"] or "", user["email"] or "",
    )
    return res.get("agent_id") or await get_user_assistant_agent_id_by_uid(pool, uid)


async def ensure_local_assistant_instance(
    goal_id: str, uid: str, story_id: str,
) -> Optional[tuple[str, str]]:
    """Ensure the (goal, user) SHCA identity row exists for local execution. Returns
    (gateway_agent_id, aiid) or None if the user/agent can't be resolved. Safe to call
    on every checkout — the instance is created once per (goal, user)."""
    pool = get_pool()
    agent_id = await _ensure_assistant_agent(pool, uid)
    if not agent_id:
        _log.warning("local assistant: no assistant agent for uid=%s (goal=%s)", uid, goal_id)
        return None

    inst = await _resolve_user_shca_instance(goal_id, uid)
    if inst:
        return inst

    # No gateway agent for a local instance — gateway_agent_id is a stable synthetic
    # id (keys this instance's memory in agent_workspace_files; consistent across
    # local runs). thread_id NULL: the identity isn't thread-bound (the story thread
    # is tracked on stories.active_thread_id).
    actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", uid)
    if not actor_id:
        return None
    gateway_agent_id = f"{agent_id}-{goal_id}-local"
    aiid = await pool.fetchval(
        """INSERT INTO agent_instances
               (id, template_id, gateway_agent_id, goal_id, thread_id, workspace,
                model, status, owner_actor_id)
           VALUES (gen_random_uuid(), $1, $2, $3, NULL, '', '', 'active', $4)
           RETURNING id::text""",
        agent_id, gateway_agent_id, goal_id, actor_id,
    )
    try:
        from app.ledger.actors import register_agent_instance_actor  # noqa: PLC0415
        await register_agent_instance_actor(uuid.UUID(aiid))
    except Exception as exc:  # non-fatal — attribution can still resolve via owner
        _log.debug("local assistant: actor pre-register skipped for %s: %s", aiid, exc)
    _log.info("local assistant: provisioned instance %s (gw=%s) goal=%s uid=%s",
              aiid, gateway_agent_id, goal_id, uid)
    return (gateway_agent_id, aiid)


async def bind_local_story_thread(goal_id: str, uid: str, story_id: str) -> None:
    """Give this story its OWN assistant thread and start it.

    THE THREAD IS A RECORD, NOT AN INBOX. A local story's conversation
    happens in the user's editor; this thread is where it is kept so the web
    UI, the GO and the next session can read it. Nothing the agent does is
    driven by it.

    ONE THREAD PER STORY, via `story_threads.ensure_story_thread` -- the same
    resolver the gateway path uses. This used to resolve a single (goal, user)
    thread and then run:

        UPDATE stories SET is_active_focus = false, active_thread_id = NULL
         WHERE active_thread_id = $1 AND id <> $2

    so importing a third story silently detached the first two: their turns
    failed `no_active_thread`, their recent-messages came back empty and their
    diffs submitted against a NULL thread. With a thread per story there is
    nothing to detach, and the UPDATE is gone rather than made conditional.

    AND NO BUSY-WITHHOLD. The old code left a second story `accepted` when the
    shared thread was busy, because one rotating thread could only serve one
    story at a time. That withheld the `in_progress` transition the git
    engine's lazy branch cut depends on, so the extension waited ~36s and
    reported "the engine hasn't cut this story's feature branch yet" -- a
    branch error for what was really a thread-binding refusal. There is no
    rotation now. Concurrency is bounded upstream in `claim_checkout` by
    `thread_ceiling`, which is the right and only place for it.
    """
    pool = get_pool()
    agent_id = await _ensure_assistant_agent(pool, uid)
    if not agent_id:
        _log.warning("local bind: no assistant agent for uid=%s (goal=%s)", uid, goal_id)
        return
    await pool.execute(
        "INSERT INTO agent_contexts (goal_id, agent_id, added_by) VALUES ($1, $2, $3) "
        "ON CONFLICT DO NOTHING",
        goal_id, agent_id, uid,
    )

    from app.services.story_threads import ensure_story_thread  # noqa: PLC0415
    thread_id = await ensure_story_thread(pool, uid, goal_id, agent_id, story_id)

    await pool.execute(
        "UPDATE stories SET active_thread_id = $1, is_active_focus = true, updated_at = now() "
        "WHERE id = $2",
        thread_id, story_id,
    )
    old_state = await pool.fetchval("SELECT state FROM stories WHERE id = $1", story_id)
    if old_state in ("proposed", "accepted"):
        await pool.execute(
            "UPDATE stories SET state = 'in_progress', started_at = COALESCE(started_at, now()), "
            "updated_at = now() WHERE id = $1 AND state IN ('proposed', 'accepted')",
            story_id,
        )
        try:
            from app.services import go_events  # noqa: PLC0415
            await go_events.on_task_state_changed(goal_id, story_id, old_state, "in_progress", uid)
        except Exception as exc:  # non-fatal
            _log.debug("local bind: GO notify skipped for %s: %s", story_id, exc)
