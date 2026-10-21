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
    # INSIDE A SAVEPOINT, and the `except` below is why it has to be.
    #
    # This runs within the CALLER'S transaction, from `append_event`'s
    # provenance audit. A statement that errors there poisons the whole
    # transaction before control ever reaches an `except` -- so catching
    # the exception here returned True, looked handled, and the caller's
    # next INSERT still died with `InFailedSQLTransactionError` naming
    # neither this query nor this table. `journal.py` describes that
    # exact failure; it is the reason the audit stopped being an import
    # and became a host callable, and the callable kept the bug.
    #
    # The one database where `actors` does not exist is the one this
    # bites on: a consumer holding only the ledger tables, which is what
    # the ledger client's own tests create. Observed when a test module
    # that imports `app.ledger` is collected alongside them -- the audit
    # is installed process-wide at import, and then fires against a
    # schema that never had an `actors` table.
    #
    # A nested `conn.transaction()` is a SAVEPOINT: the failure rolls
    # back to it and the caller's transaction survives intact.
    try:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM actors WHERE id = $1", actor_id)
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


async def human_principal_for_thread(conn, thread_id) -> Optional[uuid.UUID]:
    """The PERSON an agent on this thread is acting for, or None.

    WHY AN AGENT EVENT NEEDS ONE. An assistant message attributed only to the
    assistant is attributable to a robot and to nobody — the same dead end
    that `system` attribution was. The assistant exists for exactly one
    person, so naming them costs a join and makes 219 messages traceable.

    STRUCTURAL, NOT GUESSED, and the distinction decided how this is written.
    `agent_instances.owner_actor_id` is the binding created when the instance
    was provisioned: an SHCA is per-(goal, user) by construction, so its owner
    is a fact about the instance rather than an inference from who happens to
    have spoken. All 33 instances here carry one and all 33 point at a `user`.

    `threads.created_by` is the fallback and it is NOT trusted blindly: that
    column also holds 'engine', 'go-KER15' and 'system', so it is joined
    through `actors` with `type = 'user'` and yields NULL for all of them.
    Returning None is the correct answer for a thread nobody owns; inventing
    a principal would put a false name in an append-only record.

    DELIBERATELY NOT USED FOR THE GO. The orchestrator is goal-level and
    serves every member, so there is no one person it acts for. Attributing
    its messages to whoever created the thread would read as fact and be a
    guess — `context_goal_id` already says which goal, which is the true
    scope of what it did.
    """
    if not thread_id:
        return None
    try:
        return await conn.fetchval(
            """SELECT COALESCE(owner.id, creator.id)
                 FROM threads t
                 LEFT JOIN agent_instances ai
                        ON ai.gateway_agent_id = t.instance_id
                 LEFT JOIN actors owner
                        ON owner.id = ai.owner_actor_id AND owner.type = 'user'
                 LEFT JOIN actors creator
                        ON creator.uid = t.created_by AND creator.type = 'user'
                WHERE t.id = $1
                  AND COALESCE(owner.id, creator.id) IS NOT NULL
                LIMIT 1""",
            str(thread_id))
    except Exception:                                       # noqa: BLE001
        # Attribution is an enrichment; a message must still be recorded
        # when it cannot be resolved.
        _log.warning("could not resolve thread principal for %s", thread_id,
                     exc_info=True)
        return None


async def human_principal_for_uid(conn, uid) -> Optional[uuid.UUID]:
    """A person's actor id, or None. STRICT — never falls back.

    Deliberately not `human_actor`, which returns the SYSTEM actor when a uid
    does not resolve. That fallback is right for `actor_id`, where something
    must be named, and wrong for `on_behalf_of`, where it would assert an
    agent acted "on behalf of the system" — a sentence with no meaning, and
    precisely the `system` over-attribution this work exists to remove.

    None means "no principal can be named", which is a legitimate and
    countable state (`has_principal` on the resolved view), not an error.
    """
    if not uid:
        return None
    try:
        return await conn.fetchval(
            "SELECT id FROM actors WHERE type = 'user' AND uid = $1 LIMIT 1",
            str(uid))
    except Exception:                                       # noqa: BLE001
        _log.warning("human_principal_for_uid failed for %s", uid, exc_info=True)
        return None
