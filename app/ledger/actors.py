"""Canonical actor identity for ledger provenance.

ONE system actor (registered in the `actors` table), the ledger `actor_type`
vocabulary, and resolvers that map a principal — human user / agent / agent
instance — to `(actor_id, actor_type)`. Emit sites should use these helpers
instead of hardcoding a system UUID, so the ledger records *who actually acted*.

See LEDGER_PROVENANCE_REVIEW.md (workspace root) for the full remediation.

actor_type vocabulary — MUST stay within the `ledger_events.actor_type` CHECK
(migrations 001 + 014): {human, shca, independent_agent, goal_orchestrator, system}.
These are the GRANULAR ledger types. The `actors` registry uses COARSE types:
    human -> 'user' ; shca/independent_agent/goal_orchestrator -> 'agent' ; system -> 'system'
Council reviewer agents reuse `independent_agent` (the council/aspect goes in the
event payload) to avoid a CHECK-constraint migration.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional, Tuple

from app.database import get_pool

_log = logging.getLogger(__name__)

# The single system principal. Registered as an `actors` row (type='system') by
# migration `ledger_system_actor.sql`. Import this everywhere instead of
# re-declaring the literal (previously done ~20× with two divergent values).
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Ledger actor_type values (each must be allowed by the ledger_events CHECK).
ACTOR_HUMAN = "human"
ACTOR_SHCA = "shca"
ACTOR_IA = "independent_agent"
ACTOR_GO = "goal_orchestrator"
ACTOR_SYSTEM = "system"

# agents.type (SHCA/IA/GO/RA) -> ledger actor_type. RA (reviewer) reuses
# independent_agent; the council/aspect is recorded in the event payload.
_AGENT_TYPE_TO_ACTOR_TYPE = {
    "SHCA": ACTOR_SHCA,
    "IA": ACTOR_IA,
    "GO": ACTOR_GO,
    "RA": ACTOR_IA,
}

Actor = Tuple[uuid.UUID, str]

# In-process cache of instances already registered in `actors` this run — avoids
# a redundant idempotent INSERT on every high-frequency emit.
_registered_instances: set[str] = set()


def system_actor() -> Actor:
    """The canonical, registered system principal."""
    return SYSTEM_ACTOR_ID, ACTOR_SYSTEM


async def human_actor(uid: Optional[str]) -> Actor:
    """(users.actor_id, 'human') for a Firebase uid; system fallback if unknown."""
    if uid:
        try:
            actor_id = await get_pool().fetchval(
                "SELECT actor_id FROM users WHERE uid = $1", uid,
            )
            if actor_id:
                return actor_id, ACTOR_HUMAN
        except Exception:
            _log.warning("human_actor: lookup failed for uid=%s", uid, exc_info=True)
    return system_actor()


async def agent_actor(agent_id: Optional[str]) -> Actor:
    """(agents.actor_id, ledger type) for an agent id — a template slug or a GO
    instance slug like 'go-<goal>'; system fallback if unknown."""
    if agent_id:
        try:
            row = await get_pool().fetchrow(
                "SELECT actor_id, type FROM agents WHERE id = $1", agent_id,
            )
            if row and row["actor_id"]:
                return row["actor_id"], _AGENT_TYPE_TO_ACTOR_TYPE.get(row["type"], ACTOR_IA)
        except Exception:
            _log.warning("agent_actor: lookup failed for agent_id=%s", agent_id, exc_info=True)
    return system_actor()


async def go_actor(goal_id: Optional[str]) -> Actor:
    """(GO agent actor, 'goal_orchestrator') for a goal — resolving the GO's real
    agent id CASE-INSENSITIVELY. The gateway lowercases agent ids, so a GO for
    goal 'CWT52' has agents.id 'go-cwt52'; constructing f"go-{goal_id}" from the
    uppercase goal id would miss and fall back to system. System fallback if the
    GO row is absent."""
    if goal_id:
        try:
            row = await get_pool().fetchrow(
                "SELECT actor_id, type FROM agents "
                "WHERE lower(id) = lower($1) ORDER BY (id = $1) DESC LIMIT 1",
                f"go-{goal_id}",
            )
            if row and row["actor_id"]:
                return row["actor_id"], _AGENT_TYPE_TO_ACTOR_TYPE.get(row["type"], ACTOR_GO)
        except Exception:
            _log.warning("go_actor: lookup failed for goal=%s", goal_id, exc_info=True)
    return system_actor()


async def story_shca_actor(story_id: Optional[str]) -> Actor:
    """(SHCA instance actor, 'shca') for a story's bound SHCA, resolved from
    stories.shca_instance_id (the CP-written gateway agent id — matched the same
    id::text OR gateway_agent_id way the dashboard cost MVs use). Used for
    claude-code sentinel/DAG events (phase_task.*, dag.*), which the SHCA
    produces while executing the story. System fallback if no SHCA is bound."""
    if story_id:
        try:
            pool = get_pool()
            row = await pool.fetchrow(
                "SELECT ai.id, a.type FROM stories s "
                "JOIN agent_instances ai "
                "  ON (ai.id::text = s.shca_instance_id OR ai.gateway_agent_id = s.shca_instance_id) "
                "JOIN agents a ON a.id = ai.template_id "
                "WHERE s.id = $1 AND a.type = 'SHCA' "
                "  AND s.shca_instance_id IS NOT NULL AND s.shca_instance_id <> '' "
                "ORDER BY ai.created_at DESC LIMIT 1",
                story_id,
            )
            if row and row["id"]:
                await _ensure_instance_actor(pool, row["id"])
                return row["id"], _AGENT_TYPE_TO_ACTOR_TYPE.get(row["type"], ACTOR_SHCA)
        except Exception:
            _log.warning("story_shca_actor: lookup failed for story=%s", story_id, exc_info=True)
    return system_actor()


async def instance_actor(*, task_id: str = "", gateway_agent_id: str = "",
                         instance_id: str = "") -> Actor:
    """(agent_instances.id, ledger type) for a live instance, ensuring it has an
    `actors` row (idempotent). Resolution order:
      1. instance_id  → that exact instance
      2. task_id      → the IA/RA instance dispatched for the task
      3. gateway_agent_id → the instance behind a gateway agent id
      4. task_id      → the task's story's SHCA instance ("the assistant it runs
         on"). claude-code *phase* tasks have NO instance of their own — they run
         on the story's SHCA — so this is the RIGHT owner, not a miss. IA tasks
         only reach here if their instance was reaped, where the coordinating
         SHCA is still the honest owner.
    System is the last resort (no task/story/SHCA) — never an unregistered id
    (unlike the legacy uuid5 fallback in task_controller.instance_actor_id)."""
    pool = get_pool()
    row = None
    try:
        if instance_id:
            row = await pool.fetchrow(
                "SELECT ai.id, a.type FROM agent_instances ai "
                "JOIN agents a ON a.id = ai.template_id WHERE ai.id = $1",
                uuid.UUID(str(instance_id)),
            )
        if row is None and task_id:
            row = await pool.fetchrow(
                "SELECT ai.id, a.type FROM agent_instances ai "
                "JOIN agents a ON a.id = ai.template_id "
                "WHERE ai.task_id = $1 AND ai.status = 'active' "
                "ORDER BY ai.created_at DESC LIMIT 1",
                task_id,
            )
        if row is None and gateway_agent_id:
            row = await pool.fetchrow(
                "SELECT ai.id, a.type FROM agent_instances ai "
                "JOIN agents a ON a.id = ai.template_id "
                "WHERE ai.gateway_agent_id = $1 ORDER BY ai.created_at DESC LIMIT 1",
                gateway_agent_id,
            )
        if row is None and task_id:
            # The task's story's SHCA — "the assistant the task runs on". The
            # id::text OR gateway_agent_id match mirrors the dashboard cost MVs,
            # since stories.shca_instance_id is the CP-written gateway agent id.
            row = await pool.fetchrow(
                "SELECT ai.id, a.type FROM tasks t "
                "JOIN stories s ON s.id = t.story_id "
                "JOIN agent_instances ai "
                "  ON (ai.id::text = s.shca_instance_id OR ai.gateway_agent_id = s.shca_instance_id) "
                "JOIN agents a ON a.id = ai.template_id "
                "WHERE t.id = $1 AND a.type = 'SHCA' "
                "  AND s.shca_instance_id IS NOT NULL AND s.shca_instance_id <> '' "
                "ORDER BY ai.created_at DESC LIMIT 1",
                task_id,
            )
    except Exception:
        _log.warning("instance_actor: lookup failed", exc_info=True)
        return system_actor()
    if row and row["id"]:
        await _ensure_instance_actor(pool, row["id"])
        return row["id"], _AGENT_TYPE_TO_ACTOR_TYPE.get(row["type"], ACTOR_IA)
    return system_actor()


async def principal_actor(request) -> Actor:
    """Resolve the calling agent from its per-agent JWT (get_agent_principal).
    SHCA/IA callers carry an instance id (aiid) → the instance actor; a GO
    carries only agent_id → the GO agent actor. No principal (legacy shared
    token) → system. This is what agent-called internal endpoints should use."""
    try:
        from app.internal_auth import get_agent_principal  # lazy: avoid import cycle
        p = get_agent_principal(request)
    except Exception:
        p = None
    if not p:
        return system_actor()
    if getattr(p, "agent_instance_id", None):
        return await instance_actor(instance_id=p.agent_instance_id)
    return await agent_actor(p.agent_id)


async def request_actor(request):
    """Full attribution for an agent-JWT internal endpoint, as a 3-tuple
    (actor_id, actor_type, on_behalf_of). For an SHCA/IA *instance* caller the
    on_behalf_of is the instance's owner (the human), so events read as "the
    assistant, acting for its owner". GO callers and no-principal (legacy shared
    token) → on_behalf_of None. Use this at internal endpoints that used to
    hardcode the system actor."""
    actor_id, actor_type = await principal_actor(request)
    on_behalf_of = None
    if actor_type in (ACTOR_SHCA, ACTOR_IA):
        try:
            on_behalf_of = await get_pool().fetchval(
                "SELECT owner_actor_id FROM agent_instances WHERE id = $1", actor_id,
            )
        except Exception:
            on_behalf_of = None
    return actor_id, actor_type, on_behalf_of


async def register_agent_instance_actor(instance_id) -> None:
    """Public helper to register an instance actor at creation time (optional —
    the resolvers also register lazily). Idempotent."""
    if instance_id:
        try:
            await _ensure_instance_actor(get_pool(), uuid.UUID(str(instance_id)))
        except Exception:
            _log.warning("register_agent_instance_actor failed for %s", instance_id, exc_info=True)


# Positive-only cache of actor_ids known to have an `actors` row — lets the
# emit-time audit (actor_is_registered) run one query per distinct actor per
# process and nothing thereafter. Negatives are NOT cached (an id may become
# registered later), so a later check re-queries and then caches on success.
_known_actor_ids: set[str] = set()


async def actor_is_registered(conn, actor_id) -> bool:
    """True if actor_id resolves to a row in `actors`. Used by the ledger
    emit-time provenance audit. Cheap after warmup (positive-cached). On any
    lookup error returns True so the audit never raises a false alarm."""
    if actor_id is None:
        return False
    key = str(actor_id)
    if key in _known_actor_ids:
        return True
    try:
        exists = await conn.fetchval("SELECT 1 FROM actors WHERE id = $1", actor_id)
    except Exception:
        return True  # don't false-alarm on a lookup error
    if exists:
        _known_actor_ids.add(key)
        return True
    return False


async def _ensure_instance_actor(pool, instance_id: uuid.UUID) -> None:
    """Idempotently register an `actors` row (type='agent') for an agent
    instance, so instance-attributed ledger events resolve to a named principal.
    Cached in-process so it runs at most once per instance per process."""
    key = str(instance_id)
    if key in _registered_instances:
        return
    try:
        await pool.execute(
            """INSERT INTO actors (id, type, display_name)
               SELECT ai.id, 'agent',
                      COALESCE(NULLIF(a.name, ''), ai.template_id) || ' #' || left(ai.id::text, 8)
                 FROM agent_instances ai
                 JOIN agents a ON a.id = ai.template_id
                WHERE ai.id = $1
               ON CONFLICT (id) DO NOTHING""",
            instance_id,
        )
        _registered_instances.add(key)
    except Exception:
        _log.warning("_ensure_instance_actor failed for %s", instance_id, exc_info=True)
