"""Push-on-write fan-out for skill mutations.

When a skill is created / updated / deprecated / restored or its
`applicable_to` changes, propagate the change to every live
`agent_instances` row whose template type matches.

Gateway calls run async (asyncio.create_task) from the calling route so
the HTTP response returns immediately. Failures are logged; agents
reconcile on next provisioning via the template's standard creation
flow, so a transient gateway failure is recoverable.
"""
import asyncio
import logging
from typing import Optional

from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError


_log = logging.getLogger(__name__)


async def _live_instances_for_types(types: list[str]) -> list[dict]:
    """Every live agent whose template type is in `types`.

    The framework keeps two runtime shapes:

      • GO and per-user SHCA: an `agents` row carries the runtime
        identity. `agents.template_id` links it to the template type.

      • Per-task IA / RA: an `agent_instances` row carries the runtime,
        with `ai.template_id` → agents.id of the role-template (a row
        that lives in agents because of the legacy FK). Those legacy
        template rows have a matching row in `templates` with the same id.

    To find every live recipient of a skill push we collect both shapes.
    The query returns `gateway_agent_id` (= the openclaw agentId used
    by RPC calls) plus a template_type + goal_id for logging.
    """
    if not types:
        return []
    pool = get_pool()
    rows = await pool.fetch(
        """
        -- DISTINCT ON collapses duplicate rows for the same gateway agent — duplicate
        -- provisioning leaves many agent_instances rows per gateway_agent_id, which
        -- otherwise made skill_sync push the same skill to the same agent 10-20x.
        SELECT DISTINCT ON (gateway_agent_id) gateway_agent_id, template_type, goal_id
          FROM (
            -- Path 1: real gateway agents carried on the agents row (GO, Platform,
            -- PlatformAssistant). EXCLUDES user-owned SHCA base rows: those have NO
            -- standalone gateway agent (the real agents are per-thread instances,
            -- covered by Path 2), so pushing to them fails "unknown agent id".
            SELECT a.id AS gateway_agent_id,
                   COALESCE(t.type, a.type) AS template_type,
                   NULL::TEXT               AS goal_id
              FROM agents a
              LEFT JOIN templates t ON t.id = a.template_id
             WHERE a.status = 'active'
               AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = a.id)
               AND COALESCE(t.type, a.type) = ANY($1)
               AND COALESCE(t.type, a.type) <> 'SHCA'
               -- Skip GO agents whose goal is deleted/missing (zombie from a failed teardown).
               AND NOT (COALESCE(t.type, a.type) = 'GO'
                        AND NOT EXISTS (SELECT 1 FROM goals g
                                         WHERE lower(g.id) = lower(substr(a.id, 4))
                                           AND g.status = 'active'))
            UNION ALL
            -- Path 2: per-thread SHCA + per-task IA/RA instances.
            SELECT ai.gateway_agent_id,
                   a.type     AS template_type,
                   ai.goal_id AS goal_id
              FROM agent_instances ai
              JOIN agents a ON a.id = ai.template_id
             WHERE a.type = ANY($1)
               AND ai.status = 'active'
               AND ai.gateway_agent_id IS NOT NULL AND ai.gateway_agent_id <> ''
               -- Only live goals — never push to instances of a deleted goal.
               AND EXISTS (SELECT 1 FROM goals g
                            WHERE g.id = ai.goal_id AND g.status = 'active')
          ) sub
         ORDER BY gateway_agent_id
        """,
        types,
    )
    return [dict(r) for r in rows]


async def push_skill_to_instances(
    skill_name: str,
    content: str,
    template_types: list[str],
) -> None:
    """Push SKILL.md to every live instance of any of the given template types."""
    instances = await _live_instances_for_types(template_types)
    if not instances:
        _log.info(
            "skill push %r: no live instances for types %r",
            skill_name, template_types,
        )
        return
    for inst in instances:
        gw_id = inst["gateway_agent_id"]
        try:
            await gateway_admin.set_agent_skill(gw_id, skill_name, content)
            _log.info(
                "skill push %r → %s (template_type=%s goal=%s)",
                skill_name, gw_id, inst["template_type"], inst["goal_id"],
            )
        except GatewayError as exc:
            _log.warning("skill push %r → %s failed: %s", skill_name, gw_id, exc)
        except Exception as exc:
            _log.exception(
                "skill push %r → %s unexpected error: %s",
                skill_name, gw_id, exc,
            )


async def delete_skill_from_instances(
    skill_name: str,
    template_types: list[str],
) -> None:
    """Delete a skill's workspace dir on every live instance of any matching template type."""
    instances = await _live_instances_for_types(template_types)
    if not instances:
        return
    for inst in instances:
        gw_id = inst["gateway_agent_id"]
        try:
            await gateway_admin.delete_agent_skill(gw_id, skill_name)
            _log.info("skill delete %r → %s", skill_name, gw_id)
        except GatewayError as exc:
            _log.warning("skill delete %r → %s failed: %s", skill_name, gw_id, exc)
        except Exception as exc:
            _log.exception(
                "skill delete %r → %s unexpected error: %s",
                skill_name, gw_id, exc,
            )


async def propagate_skill_mutation(
    skill_name: str,
    content: str,
    old_applicable_to: Optional[list[str]],
    new_applicable_to: list[str],
    new_status: str,
) -> None:
    """Diff the old vs new state and dispatch pushes / deletes accordingly.

    Cases handled:
      • deprecated         → delete from every type the skill *had* been on
      • added to a type    → push to that type's live instances
      • removed from a type → delete from that type's live instances
      • content changed (no applicable_to change) → push to every current type
      • restored from deprecated → push to every type in new_applicable_to
    """
    old_set = set(old_applicable_to or [])
    new_set = set(new_applicable_to or [])

    if new_status == "deprecated":
        # Skill was previously active and is now deprecated. Remove it
        # from every template type that ever had it. Using union(old, new)
        # is defensive — if old was empty (newly-created-then-deprecated),
        # new is still cleaned up.
        all_types = sorted(old_set | new_set)
        await delete_skill_from_instances(skill_name, all_types)
        return

    # Active state: push to current types, delete from any that were dropped.
    removed = sorted(old_set - new_set)
    if removed:
        await delete_skill_from_instances(skill_name, removed)
    if new_set:
        await push_skill_to_instances(skill_name, content, sorted(new_set))


async def reconcile_skills_on_startup() -> None:
    """Propagate ONLY skills whose content changed since the last reconcile.

    Purpose: catch skill edits that landed via a migration (which bypass the
    admin push-on-write fan-out). New instances already receive every skill at
    creation, and admin edits push themselves — so a blanket "re-push everything
    to everyone on every boot" is pure churn. We compare each active skill's
    content_hash against `skill_push_state`; unchanged skills are skipped entirely,
    changed/new skills are fanned out and their hash recorded.
    """
    pool = get_pool()
    skills = await pool.fetch(
        "SELECT name, content, content_hash, applicable_to FROM skills WHERE status = 'active'"
    )
    if not skills:
        _log.info("skill reconciliation: no active skills found — nothing to push")
        return

    prev = {
        r["skill_name"]: r["content_hash"]
        for r in await pool.fetch("SELECT skill_name, content_hash FROM skill_push_state")
    }

    pushed = skipped = failed = 0
    for skill in skills:
        name = skill["name"]
        chash = skill["content_hash"]
        if prev.get(name) == chash:
            skipped += 1
            continue
        try:
            await push_skill_to_instances(name, skill["content"], list(skill["applicable_to"]))
            await pool.execute(
                """INSERT INTO skill_push_state (skill_name, content_hash, updated_at)
                   VALUES ($1, $2, now())
                   ON CONFLICT (skill_name)
                   DO UPDATE SET content_hash = EXCLUDED.content_hash, updated_at = now()""",
                name, chash,
            )
            pushed += 1
        except Exception as exc:
            _log.warning("skill reconciliation: failed to push %r: %s", name, exc)
            failed += 1
    _log.info(
        "skill reconciliation: done — changed/pushed=%d skipped(unchanged)=%d failed=%d",
        pushed, skipped, failed,
    )


def schedule_propagation(
    skill_name: str,
    content: str,
    old_applicable_to: Optional[list[str]],
    new_applicable_to: list[str],
    new_status: str,
) -> asyncio.Task:
    """Fire-and-forget wrapper. Returns the task so callers can await it
    in tests; in production, callers just discard it.
    """
    return asyncio.create_task(propagate_skill_mutation(
        skill_name=skill_name,
        content=content,
        old_applicable_to=old_applicable_to,
        new_applicable_to=new_applicable_to,
        new_status=new_status,
    ))
