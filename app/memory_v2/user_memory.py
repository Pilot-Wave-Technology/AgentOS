"""Cross-goal user memory — USER_MEMORY.md symlinked into every SHCA workspace.

The master file lives at `~/.openclaw/user-memory/<uid>/USER_MEMORY.md`
on the openclaw VM. Each of the user's SHCAs gets a symlink at
`<workspace>/USER_MEMORY.md` so the bootstrap loader auto-injects the
same content into every one of that user's agents — closing the
cross-goal recall gap.

Both operations are idempotent (safe to call repeatedly at provisioning).
"""
from __future__ import annotations

import logging

from app.openclaw import gateway_admin, GatewayError


_log = logging.getLogger(__name__)


async def ensure_user_memory_file(user_uid: str) -> bool:
    """Idempotent — create the master USER_MEMORY.md for a user if it
    doesn't exist. Returns True if the call succeeded, False on failure.

    Non-fatal failure: if the gateway is down we log and skip; the agent
    just won't have USER_MEMORY.md until next provision.
    """
    if not user_uid:
        return False
    try:
        await gateway_admin.call(
            "memory.ensure_user_file", {"userUid": user_uid}, timeout=15,
        )
        return True
    except GatewayError as exc:
        _log.warning(
            "ensure_user_memory_file: gateway error for user=%s: %s",
            user_uid, exc,
        )
        return False
    except Exception:
        _log.exception(
            "ensure_user_memory_file: failed for user=%s — non-fatal",
            user_uid,
        )
        return False


async def link_user_memory_into_workspace(agent_id: str, user_uid: str) -> bool:
    """Create or refresh the USER_MEMORY.md symlink in the agent's workspace.

    Called from SHCA provisioning right after the agent is created.
    Idempotent: if the symlink already points at the right target, no-op.

    Non-fatal failure: workspace just won't have USER_MEMORY.md.
    """
    if not agent_id or not user_uid:
        return False
    try:
        result = await gateway_admin.call(
            "memory.symlink_user_file",
            {"agentId": agent_id, "userUid": user_uid},
            timeout=15,
        )
        _log.info(
            "user_memory: linked for agent=%s user=%s (linked=%s)",
            agent_id, user_uid, result.get("linked"),
        )
        return True
    except GatewayError as exc:
        _log.warning(
            "link_user_memory_into_workspace: gateway error for agent=%s user=%s: %s",
            agent_id, user_uid, exc,
        )
        return False
    except Exception:
        _log.exception(
            "link_user_memory_into_workspace: failed for agent=%s user=%s — non-fatal",
            agent_id, user_uid,
        )
        return False


async def setup_user_memory_for_agent(agent_id: str, user_uid: str) -> bool:
    """Convenience: ensure the master file exists, then symlink it into
    the agent workspace. Called from SHCA provisioning at the end of the
    setup flow. Returns True on full success.
    """
    ok = await ensure_user_memory_file(user_uid)
    if not ok:
        return False
    return await link_user_memory_into_workspace(agent_id, user_uid)
