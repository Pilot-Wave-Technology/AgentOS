"""Force the gateway's live sessions for an agent to re-resolve their model.

Closes a gap discovered repeatedly this session: `agents.update` (the
gateway RPC both `model_change.py::change_agent_model` and
`agent_registry/update.py::update_agent` call after a model change) only
rewrites the agent's *persisted config* — it never touches an already-
running session's cached `SessionEntry.model`, which the gateway sets
once at session-create/reset time and never re-syncs against later config
changes (confirmed via `agent_os_openclaw`'s `agents.ts`/`sessions.ts`). Left
alone, a chat that was already open when the model changed keeps
reporting (and using) the OLD model indefinitely — this was hand-fixed
via a manual `sessions.reset` call after nearly every model-change this
session; this module makes that automatic instead.
"""
import logging

from app.database import get_pool
from app.openclaw import gateway_admin

logger = logging.getLogger(__name__)


async def resolve_gateway_agent_id(agent_id: str, goal_id: str, thread_id: str) -> str:
    """Map a chat-URL agent id to the actual gateway agent id.

    Both SHCA and GO are template→instance: chat URLs carry the template
    id (SHCA: "abhishek-shca"; GO: "go-<goal>") but the gateway addresses
    the per-instance gateway_agent_id from agent_instances. For SHCA the
    two differ (instance gets a goal+uuid suffix); for GO they currently
    match — but only by coincidence. Always resolve through
    agent_instances; don't special-case either type.

    Falls back to the input unchanged if no active agent_instances row
    matches — defensive only, indicates a provisioning gap.

    Moved here (from `memory_v2/routers.py`, which now re-exports it) once
    `reset_agent_sessions` below became a second consumer — shared logic
    belongs in a service module, not reached into a router's private
    function.
    """
    try:
        pool = get_pool()
        # The agent's LIVE session runs under its RAW instance id — the platform
        # records that verbatim on the thread (threads.instance_id) and builds
        # every session key from it (session._gateway_agent_id = instance_id).
        # That string is deliberately NOT the same as agent_instances.gateway_
        # agent_id, which stores the gateway-NORMALIZED id (lowercased + truncated
        # to 64 chars) because token_calls.agent_id echoes the normalized form for
        # cost joins. But the gateway persists the session ENTRY under the raw key
        # (toAgentStoreSessionKey keeps the key's agent part verbatim), so for the
        # usage/compact session key we MUST use the raw id — otherwise
        # sessions.status looks under a key the gateway never created and the chat
        # context-usage ring comes up empty (only worked before when a row happened
        # to still hold the raw id). Prefer threads.instance_id; fall back to the
        # agent_instances lookups below for GO / legacy rows with no thread instance.
        if thread_id:
            # Guard on the agent prefix so a shared/handed-off thread can only
            # ever return THIS agent's instance (instance_id is built as
            # f"{agent_id}-{goal_id}-{thread_id}", so its prefix is agent_id).
            inst = await pool.fetchval(
                """SELECT instance_id FROM threads
                    WHERE id = $1 AND starts_with(instance_id, $2 || '-')""",
                thread_id, agent_id,
            )
            if inst:
                return inst
        row = await pool.fetchrow(
            """
            SELECT gateway_agent_id
              FROM agent_instances
             WHERE template_id = $1
               AND thread_id = $2
               AND status = 'active'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            agent_id, thread_id,
        )
        if row and row["gateway_agent_id"]:
            return row["gateway_agent_id"]
        # Fallback: try by goal alone (covers SHCAs not yet bound to a
        # specific thread).
        if goal_id:
            row = await pool.fetchrow(
                """
                SELECT gateway_agent_id
                  FROM agent_instances
                 WHERE template_id = $1
                   AND LOWER(goal_id) = LOWER($2)
                   AND status = 'active'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                agent_id, goal_id,
            )
            if row and row["gateway_agent_id"]:
                return row["gateway_agent_id"]
    except Exception:
        logger.exception("resolve_gateway_agent_id failed for %s", agent_id)
    return agent_id


async def _evict_local_sessions(agent_id: str) -> int:
    """Evict this process's own in-memory `_AgentSession` objects for
    `agent_id`, so the next message rebuilds them against the fresh DB
    config instead of continuing to use whatever model/context the
    session was constructed with.

    This is a THIRD cache, distinct from both the DB row and the
    gateway's own SessionEntry: `session_manager.get_or_create()` returns
    the cached `_AgentSession` unchanged once a (goal, agent[, thread])
    key exists, and nothing about that object ever re-reads `agents.model`
    afterwards. Originally only `model_change.py::change_agent_model` (the
    GO path) did this eviction; `agent_registry/update.py::update_agent`
    (the SHCA / bulk-apply path) called only the gateway-side
    `sessions.reset` above and missed this — which is exactly why a model
    change could show up in the DB and even survive a gateway reset, yet
    an already-open chat tab kept using the old model until the browser
    reconnected. Folded in here so every caller of `reset_agent_sessions`
    gets both evictions together and this gap can't reopen per-caller.

    Returns the number of sessions evicted.
    """
    from app.openclaw import session_manager
    keys_to_evict = [
        key for key in list(session_manager._sessions.keys())
        if isinstance(key, tuple)
        and (
            # SHCA: key is (goal_id, template_agent_id, thread_id)
            (len(key) == 3 and key[1] == agent_id)
            # GO / legacy IA: key is (goal_id, agent_id) — 2-tuple
            or (len(key) == 2 and key[1] == agent_id)
        )
    ]
    for key in keys_to_evict:
        session = session_manager._sessions.pop(key, None)
        if session is not None:
            try:
                await session.stop()
            except Exception:
                pass
    return len(keys_to_evict)


async def reset_agent_sessions(agent_id: str) -> int:
    """Reset every live session for `agent_id`, across THREE separate
    caches: this process's local `_AgentSession` cache, the gateway's
    per-THREAD-INSTANCE agent config, and the gateway's SessionEntry
    cache for every open/active thread.

    The middle one is the gap that made this whole function insufficient
    on its own: for SHCA, each thread gets its OWN gateway agent identity
    (e.g. "john-doe-assistant-9b8a-T2EST-<thread-uuid>"), separate from
    the base agent id that `update_agent()`/`change_agent_model()` call
    `agents.update` on. `sessions.reset` rebuilds a session's model from
    THAT per-instance identity's own registration — never from the base
    identity — so resetting a session without first pushing the new
    model to its per-instance identity just recreates the SAME stale
    session (confirmed live: a thread whose per-instance identity already
    had an explicit model override kept using it through repeated resets
    that all reported success, while a thread with no such override
    happened to read current config and looked fine — coincidence, not
    correctness). Push model to every resolved per-instance id before
    resetting, so this can't happen regardless of whether a given thread
    happens to have a per-instance override yet.

    Soft-fail throughout, per-thread: one thread's gateway hiccup must
    not stop the others from resetting. Returns the gateway-reset count
    (for logging by the caller — never raises).
    """
    try:
        evicted = await _evict_local_sessions(agent_id)
        if evicted:
            logger.info(
                "reset_agent_sessions: evicted %d local session(s) for agent=%s",
                evicted, agent_id,
            )
    except Exception as exc:
        logger.warning(
            "reset_agent_sessions: local session evict failed for agent=%s: %s",
            agent_id, exc,
        )

    pool = get_pool()
    try:
        threads = await pool.fetch(
            """SELECT id AS thread_id, goal_id FROM threads
                WHERE agent_id = $1 AND status IN ('open', 'active')""",
            agent_id,
        )
    except Exception as exc:
        logger.warning(
            "reset_agent_sessions: thread lookup failed for agent=%s: %s",
            agent_id, exc,
        )
        return 0

    # Current model, to re-push per-instance below. Read fresh from the DB
    # rather than accepting it as a parameter — the caller has always
    # already committed this by the time it calls us, and reading it here
    # keeps this function self-sufficient for any future caller.
    current_model = await pool.fetchval("SELECT model FROM agents WHERE id = $1", agent_id)

    reset_count = 0
    for row in threads:
        thread_id, goal_id = row["thread_id"], row["goal_id"]
        try:
            gw_agent_id = await resolve_gateway_agent_id(agent_id, goal_id, thread_id)
            if current_model:
                try:
                    await gateway_admin.call(
                        "agents.update", {"agentId": gw_agent_id, "model": current_model},
                    )
                except Exception as exc:
                    logger.warning(
                        "reset_agent_sessions: per-instance agents.update failed for "
                        "agent=%s thread=%s gw_agent_id=%s: %s",
                        agent_id, thread_id, gw_agent_id, exc,
                    )
            session_key = (
                f"agent:{gw_agent_id}:{goal_id.lower()}-{thread_id}"
                if thread_id else f"agent:{gw_agent_id}:"
            )
            await gateway_admin.call("sessions.reset", {"key": session_key, "reason": "reset"})
            reset_count += 1
        except Exception as exc:
            logger.warning(
                "reset_agent_sessions: sessions.reset failed for agent=%s thread=%s: %s",
                agent_id, thread_id, exc,
            )

    if reset_count:
        logger.info(
            "reset_agent_sessions: reset %d/%d gateway session(s) for agent=%s",
            reset_count, len(threads), agent_id,
        )
    return reset_count
