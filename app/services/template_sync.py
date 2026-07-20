"""Push-on-write fan-out for template_files mutations.

When an admin edits a template's workspace file via /templates (e.g.
edits the GO template's AGENTS.md or SOUL.md), every live instance
of that template gets the new content pushed to its gateway workspace.

The agent picks up the new content on its next inference turn — no
restart needed. Mirrors the shape of `app.services.skill_sync` but
targets the openclaw `agents.files.set` RPC instead of
`agents.skills.set`.

Per-instance IDENTITY.md is NEVER overwritten by this fan-out — it's
computed at provisioning time and carries the per-instance Model line.
Template edits to IDENTITY.md (if any) seed new instances but don't
overwrite existing ones.
"""
import asyncio
import logging
from typing import Optional

from app.database import get_pool
from app.openclaw import gateway_admin as _default_gateway, GatewayError


_log = logging.getLogger(__name__)


# Files that may NOT be force-overwritten on live instances by template
# edits. IDENTITY.md is per-instance (carries model_label); HEARTBEAT and
# BOOTSTRAP are runtime state that the gateway manages itself.
_PROTECTED_FILENAMES = frozenset({"IDENTITY.md", "HEARTBEAT.md", "BOOTSTRAP.md"})

# Files that may NOT propagate from an admin template down to per-user
# templates (Phase 0 three-tier model). USER.md and IDENTITY.md are
# personalized at the per-user template level — admin updates of these
# would clobber every user's personalization. HEARTBEAT/BOOTSTRAP are
# runtime artifacts and should never live in admin templates anyway.
_NEVER_PROPAGATE_TO_CHILD_TEMPLATES = frozenset({
    "USER.md", "IDENTITY.md", "HEARTBEAT.md", "BOOTSTRAP.md",
})


async def _get_child_user_templates(parent_template_id: str) -> list[dict]:
    """Return per-user templates parented on this admin template."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, owner_actor_id, type
          FROM templates
         WHERE parent_template_id = $1
           AND owner_actor_id IS NOT NULL
           AND status = 'active'
        """,
        parent_template_id,
    )
    return [dict(r) for r in rows]


async def push_admin_template_file_to_user_templates(
    admin_template_id: str,
    filename: str,
    content: str,
    *,
    gateway=None,
) -> int:
    """Cascade an admin-template file edit into all per-user templates.

    Writes to template_files for each child per-user template, then
    triggers fan-out to that template's live instances.

    Returns the number of per-user templates touched.
    """
    if filename in _NEVER_PROPAGATE_TO_CHILD_TEMPLATES:
        _log.info(
            "admin→user-template cascade skipped: %s/%s is non-propagating",
            admin_template_id, filename,
        )
        return 0

    children = await _get_child_user_templates(admin_template_id)
    if not children:
        _log.info(
            "admin→user-template cascade %s/%s: no per-user children",
            admin_template_id, filename,
        )
        return 0

    pool = get_pool()
    for child in children:
        # Write to template_files for the per-user template.
        await pool.execute(
            """
            INSERT INTO template_files (template_id, filename, content, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (template_id, filename)
            DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
            """,
            child["id"], filename, content,
        )
        _log.info(
            "admin→user-template %s/%s → %s (owner=%s)",
            admin_template_id, filename, child["id"], child["owner_actor_id"],
        )
        # Then cascade to that per-user template's live instances.
        await push_template_file_to_instances(child["id"], filename, content, gateway=gateway)

    return len(children)


async def _live_instances_for_template(template_id: str) -> list[dict]:
    """Find every live agent or per-conversation instance linked to this template.

    Returns rows with gateway_agent_id (= openclaw agentId for RPC) plus
    goal_id for logging.

    Three paths into "instances of $1":
      1. agents.template_id = $1  (user-owned SHCAs after migration 038;
         per-goal GO rows after migration 037)
      2. agent_instances.template_id = $1  (legacy: when ai.template_id
         literally holds the templates.id, e.g. per-goal GO instances
         where the canonical 'go' template id matches)
      3. agent_instances.template_id → agents.id where that agent's
         template_id = $1  (per-conversation SHCA instances after
         migration 038: ai.template_id holds the user's agent_id, and
         that agent links to the per-user template via agents.template_id)
    """
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT a.id AS gateway_agent_id,
               NULL::TEXT AS goal_id
          FROM agents a
         WHERE a.template_id = $1
           AND a.status = 'active'
           AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = a.id)
        UNION
        SELECT ai.gateway_agent_id,
               ai.goal_id AS goal_id
          FROM agent_instances ai
          LEFT JOIN agents parent ON parent.id = ai.template_id
         WHERE ai.status = 'active'
           AND (ai.template_id = $1
                OR parent.template_id = $1)
        """,
        template_id,
    )
    return [dict(r) for r in rows]


async def push_template_file_to_instances(
    template_id: str,
    filename: str,
    content: str,
    *,
    gateway=None,
) -> None:
    """Push a template file to every live instance of that template."""
    gw = gateway if gateway is not None else _default_gateway
    if filename in _PROTECTED_FILENAMES:
        _log.info(
            "template push %s/%s skipped: protected filename",
            template_id, filename,
        )
        return
    instances = await _live_instances_for_template(template_id)
    if not instances:
        _log.info(
            "template push %s/%s: no live instances",
            template_id, filename,
        )
        return
    for inst in instances:
        gw_id = inst["gateway_agent_id"]
        try:
            await gw.set_agent_file(gw_id, filename, content)
            _log.info(
                "template push %s/%s → %s (goal=%s)",
                template_id, filename, gw_id, inst.get("goal_id"),
            )
        except GatewayError as exc:
            _log.warning(
                "template push %s/%s → %s failed: %s",
                template_id, filename, gw_id, exc,
            )
        except Exception as exc:
            _log.exception(
                "template push %s/%s → %s unexpected error: %s",
                template_id, filename, gw_id, exc,
            )


async def delete_template_file_from_instances(
    template_id: str,
    filename: str,
    *,
    gateway=None,
) -> None:
    """Remove a template file from every live instance of that template."""
    gw = gateway if gateway is not None else _default_gateway
    if filename in _PROTECTED_FILENAMES:
        return
    instances = await _live_instances_for_template(template_id)
    if not instances:
        return
    for inst in instances:
        gw_id = inst["gateway_agent_id"]
        try:
            await gw.delete_agent_file(gw_id, filename)
            _log.info("template delete %s/%s → %s", template_id, filename, gw_id)
        except GatewayError as exc:
            _log.warning(
                "template delete %s/%s → %s failed: %s",
                template_id, filename, gw_id, exc,
            )


async def _is_admin_template(template_id: str) -> bool:
    """True if this template is admin-managed (no owner, no parent)."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM templates "
        "WHERE id = $1 AND owner_actor_id IS NULL AND parent_template_id IS NULL",
        template_id,
    )
    return row is not None


async def push_template_file(
    template_id: str,
    filename: str,
    content: str,
    *,
    gateway=None,
) -> None:
    """Top-level dispatcher for template file edits (three-tier aware).

    For admin templates: cascade to per-user templates first (which
    each cascade to their own live instances), then also push directly
    to any instances directly attached to the admin template (rare;
    admin test instances).

    For per-user templates: just push to that template's live instances.
    """
    if await _is_admin_template(template_id):
        # Cascade admin → per-user templates → instances of each.
        await push_admin_template_file_to_user_templates(
            template_id, filename, content, gateway=gateway,
        )
    # Always also push to direct instances of `template_id`.
    # For admin templates this catches admin-owned test instances.
    # For per-user templates this is the main path.
    await push_template_file_to_instances(template_id, filename, content, gateway=gateway)


def schedule_file_push(
    template_id: str,
    filename: str,
    content: str,
    *,
    gateway=None,
) -> asyncio.Task:
    """Fire-and-forget wrapper for use from HTTP routes.

    Three-tier aware: admin template edits cascade to per-user templates
    before pushing to instances.
    """
    return asyncio.create_task(
        push_template_file(template_id, filename, content, gateway=gateway)
    )


def schedule_file_delete(template_id: str, filename: str, *, gateway=None) -> asyncio.Task:
    return asyncio.create_task(
        delete_template_file_from_instances(template_id, filename, gateway=gateway)
    )
