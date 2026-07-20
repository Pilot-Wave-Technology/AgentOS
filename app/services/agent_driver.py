"""Headless agent session driver — start GO and SHCA sessions without a browser.

The session model in `app/openclaw.py` was designed around browser WebSocket
subscribers: sessions are created when a WS connects and are evicted after 30
minutes with no subscribers. This module provides server-side entry points so
Slack (or future channels) can drive agents without requiring the web UI to
be open.

Public API:
  - `ensure_go_session(goal_id)` → session for the goal's GO thread
  - `ensure_shca_session(goal_id, agent_id, thread_id)` → SHCA session

Both reuse the exact callbacks the web UI uses (imported from the router
modules) so message persistence, event appending, and GO action processing
all behave identically.
"""
import logging

from app.database import get_pool
from app.openclaw import session_manager

_log = logging.getLogger(__name__)


async def ensure_go_session(goal_id: str):
    """Return the GO `_AgentSession` for the goal, creating it if missing.

    Returns None if the goal has no GO agent or GO thread wired up.
    """
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT id, status, go_agent_id, go_thread_id FROM goals WHERE id = $1", goal_id,
    )
    if not goal or goal.get("status") != "active":
        _log.info("ensure_go_session: goal %s not active (status=%s) — not warming a GO session",
                  goal_id, (goal or {}).get("status"))
        return None
    if not goal.get("go_agent_id") or not goal.get("go_thread_id"):
        _log.debug("ensure_go_session: goal %s has no GO wiring", goal_id)
        return None

    go_agent_id = goal["go_agent_id"]
    go_thread_id = goal["go_thread_id"]

    key = (goal_id, go_agent_id)
    existing = session_manager._sessions.get(key)
    if existing:
        return existing

    from app.routers.goal_thread import _get_go_context, _make_go_callbacks

    goal_context = await _get_go_context(pool, goal_id, go_agent_id)
    on_message, on_notify = _make_go_callbacks(
        pool, goal_id, go_agent_id, go_thread_id,
    )
    session = session_manager.get_or_create(
        goal_id, go_agent_id, goal_context, on_message, on_notify,
        thread_id=go_thread_id,
    )

    # IMPORTANT: the gateway often returns a lowercased agent id
    # (e.g. control-panel agent_id=go-BMIWA → gateway id=go-bmiwa,
    # because agents.create lowercases). _AgentSession.send_to_gateway
    # builds the session-key from `_gateway_agent_id` when set (else
    # falls back to self.agent_id), so if we don't stamp the gateway
    # id here, outgoing chat.send uses the uppercase name and the
    # gateway's response events route under the lowercase name — our
    # session never sees them and the agent's reply disappears.
    # Same pattern applies to SHCA per-conversation instance ids.
    # goal_thread.py sets this after get_or_create; so must we.
    gw_id = (goal_context or {}).get("gateway_agent_id", "") or ""
    if gw_id:
        session._gateway_agent_id = gw_id

    _log.info(
        "Headless-started GO session for goal %s (agent=%s gw=%s)",
        goal_id, go_agent_id, gw_id or "-",
    )
    return session


async def ensure_shca_session(goal_id: str, agent_id: str, thread_id: str):
    """Return the SHCA `_AgentSession` for (goal, agent, thread), creating it if missing.
    Returns None if the goal has been deleted (nothing should warm for a dead goal)."""
    pool = get_pool()

    from app.services.goal_guard import goal_is_dead
    if await goal_is_dead(goal_id, pool):
        _log.info("ensure_shca_session: goal %s deleted — not warming an SHCA session", goal_id)
        return None

    key = (goal_id, agent_id, thread_id)
    existing = session_manager._sessions.get(key)
    if existing:
        return existing

    from app.routers.chat import _get_goal_context, _make_callbacks

    goal_context = await _get_goal_context(pool, goal_id, agent_id)
    on_message, on_notify = _make_callbacks(pool, goal_id, agent_id)
    session = session_manager.get_or_create(
        goal_id, agent_id, goal_context, on_message, on_notify, thread_id=thread_id,
    )
    _log.info(
        "Headless-started SHCA session: goal=%s agent=%s thread=%s",
        goal_id, agent_id, thread_id[:12],
    )
    return session
