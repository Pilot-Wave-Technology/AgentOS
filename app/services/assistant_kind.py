"""Which kind of assistant is this — the operator, or the kernel one?

The two differ in ONE way that the control panel has to care about: where
the work happens.

The operator does the work itself. Its story workspace on the gateway is
the working tree — Claude Code is spawned in it, commits come out of it,
and the clone is the point.

The kernel assistant delegates. It starts a run, the Conductor admits it,
and an executor works in its own sandbox with its own checkout. The
assistant reads that tree through the `workspace` view. A clone in the
assistant's gateway workspace is a second copy nobody reads and nothing
updates.

Everything Stage K does to provisioning hangs off this one question, so it
is asked in one place. Five call sites each running their own
`template_id = 'kernel-assistant'` query is how a surface acquires five
declarations and then drifts — the single most recurring bug class in this
refactor.

The answer is a property of the USER'S assistant, not of the goal: a
person has one assistant, provisioned from one template, and it follows
them into every goal.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# The template every kernel assistant instance is created from.
KERNEL_TEMPLATE_ID = "kernel-assistant"


async def is_kernel_assistant(pool, agent_id: str) -> bool:
    """Does this agent delegate execution to an executor sandbox?

    False for the operator, for the platform singleton, and for an agent
    id that resolves to nothing — every one of which wants the existing
    clone-based behaviour, so an unknown agent falls back to what already
    works rather than to the new path.
    """
    if not agent_id:
        return False
    return bool(await pool.fetchval(
        "SELECT 1 FROM agents WHERE id = $1 AND template_id = $2",
        agent_id, KERNEL_TEMPLATE_ID,
    ))


async def is_kernel_assistant_for_user(pool, user_uid: str) -> bool:
    """Same question, asked where only the person is known.

    The accept gate and the story-transition guards run from the board,
    which knows who is acting but has never resolved their assistant.
    Resolving it here keeps them from having to.
    """
    if not user_uid:
        return False
    from app.services.agent_registry.assistant_queries import (
        get_user_assistant_agent_id_by_uid,
    )
    try:
        agent_id = await get_user_assistant_agent_id_by_uid(pool, user_uid)
    except Exception as exc:  # a lookup failure must not block the board
        _log.warning("assistant kind lookup failed for uid=%s: %s", user_uid, exc)
        return False
    return await is_kernel_assistant(pool, agent_id or "")


# ── What that implies for the story workspace ────────────────────────────

WORKSPACE_CLONE = "clone"
WORKSPACE_DOCS = "docs"


async def workspace_kind_for(pool, agent_id: str) -> str:
    """`stories.workspace_kind` for a story provisioned against this agent."""
    return (WORKSPACE_DOCS if await is_kernel_assistant(pool, agent_id)
            else WORKSPACE_CLONE)
