"""Canonical query helpers for assistant (SHCA) agent operations.

All functions query only the canonical tables: templates, template_files,
template_history, agents. Never shca_templates, shca_template_files, or
user_shca_configs — those are deprecated.

Call sites:
  - app/assistant_provisioning.py  (template management + user provisioning)
  - app/routers/assistant.py       (admin template CRUD)
"""
import json
import logging
import uuid
from typing import Optional

from app.agent_types import ASSISTANT, to_db_agent_type

# DB type value for the assistant agent — 'SHCA' in the current schema.
# All SQL queries route through this constant; when the DB migration runs,
# only agent_types.py needs to change.
_DB_AGENT_TYPE = to_db_agent_type(ASSISTANT)

logger = logging.getLogger(__name__)


# ── Admin template queries ────────────────────────────────────────────────────

async def list_admin_assistant_templates(pool) -> list[dict]:
    """List active admin assistant templates (no owner — system-wide blueprints)."""
    rows = await pool.fetch(
        f"""SELECT id, name, description, model, status, created_at
             FROM templates
            WHERE type = '{_DB_AGENT_TYPE}'
              AND owner_actor_id IS NULL
              AND status = 'active'
            ORDER BY created_at""",
    )
    logger.debug("list_admin_assistant_templates: found %d templates", len(rows))
    return [dict(r) for r in rows]


async def get_admin_assistant_template(pool, template_id: str) -> Optional[dict]:
    """Get an admin assistant template with its files. Returns None if not found."""
    row = await pool.fetchrow(
        f"""SELECT id, name, description, model, status, created_at
             FROM templates
            WHERE id = $1
              AND type = '{_DB_AGENT_TYPE}'
              AND owner_actor_id IS NULL""",
        template_id,
    )
    if not row:
        return None
    template = dict(row)
    files = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id = $1 ORDER BY filename",
        template_id,
    )
    template["files"] = {f["filename"]: f["content"] for f in files}
    return template


async def create_admin_assistant_template(
    pool,
    template_id: str,
    name: str,
    description: str,
    model: str,
    agents_md: str,
    soul_md: str,
) -> dict:
    """Insert a new admin assistant template into templates + template_files + template_history."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"""INSERT INTO templates
                       (id, type, name, description, model, status, version)
                   VALUES ($1, '{_DB_AGENT_TYPE}', $2, $3, $4, 'active', 1)""",
                template_id, name, description, model,
            )
            file_entries = [(fn, content) for fn, content in
                            [("AGENTS.md", agents_md), ("SOUL.md", soul_md)] if content]
            for filename, content in file_entries:
                await conn.execute(
                    """INSERT INTO template_files (template_id, filename, content, updated_at)
                       VALUES ($1, $2, $3, NOW())
                       ON CONFLICT (template_id, filename)
                       DO UPDATE SET content = $3, updated_at = NOW()""",
                    template_id, filename, content,
                )
            await conn.execute(
                """INSERT INTO template_history
                       (template_id, version, snapshot, changed_by, change_reason)
                   VALUES ($1, 1, $2::jsonb, 'system', 'initial creation')""",
                template_id,
                json.dumps({
                    "type": _DB_AGENT_TYPE, "name": name, "model": model,
                    "files": {fn: c for fn, c in file_entries},
                }),
            )
    logger.info("create_admin_assistant_template: created %s (%s)", template_id, name)
    return {"id": template_id, "name": name}


async def update_admin_assistant_template_files(
    pool,
    template_id: str,
    files: dict[str, str],
    changed_by: str = "system",
) -> None:
    """Upsert template_files and bump version. Triggers fan-out via template_sync."""
    from app.services.template_sync import schedule_file_push
    async with pool.acquire() as conn:
        async with conn.transaction():
            new_version = await conn.fetchval(
                "UPDATE templates SET version = version + 1, updated_at = NOW() WHERE id = $1 RETURNING version",
                template_id,
            )
            for filename, content in files.items():
                await conn.execute(
                    """INSERT INTO template_files (template_id, filename, content, updated_at)
                       VALUES ($1, $2, $3, NOW())
                       ON CONFLICT (template_id, filename)
                       DO UPDATE SET content = $3, updated_at = NOW()""",
                    template_id, filename, content,
                )
            await conn.execute(
                """INSERT INTO template_history
                       (template_id, version, snapshot, changed_by, change_reason)
                   VALUES ($1, $2, $3::jsonb, $4, 'file update')
                   ON CONFLICT (template_id, version) DO NOTHING""",
                template_id, new_version,
                json.dumps({"files_updated": list(files.keys())}),
                changed_by,
            )
    for filename, content in files.items():
        schedule_file_push(template_id, filename, content)
    logger.info(
        "update_admin_assistant_template_files: template=%s v%s files=%s",
        template_id, new_version, list(files.keys()),
    )


# ── User assistant queries ────────────────────────────────────────────────────

async def get_user_assistant_agent_id(pool, user_actor_id: uuid.UUID) -> Optional[str]:
    """Return agent_id of the user's active assistant, or None. Keyed by actor UUID."""
    agent_id = await pool.fetchval(
        f"""SELECT id FROM agents
            WHERE owner_actor_id = $1
              AND type = '{_DB_AGENT_TYPE}'
              AND status = 'active'
            ORDER BY created_at
            LIMIT 1""",
        user_actor_id,
    )
    logger.debug("get_user_assistant_agent_id: actor=%s → %s", user_actor_id, agent_id)
    return agent_id


async def get_user_assistant_agent_id_by_uid(pool, user_uid: str) -> Optional[str]:
    """Resolve Firebase UID to the user's active assistant agent_id."""
    agent_id = await pool.fetchval(
        f"""SELECT a.id FROM agents a
             JOIN users u ON u.actor_id = a.owner_actor_id
            WHERE u.uid = $1
              AND a.type = '{_DB_AGENT_TYPE}'
              AND a.status = 'active'
            ORDER BY a.created_at
            LIMIT 1""",
        user_uid,
    )
    logger.debug("get_user_assistant_agent_id_by_uid: uid=%s → %s", user_uid, agent_id)
    return agent_id


async def get_user_assistant_template_id_by_uid(pool, user_uid: str) -> Optional[str]:
    """Return the per-user template_id for a user's assistant (used by session routing)."""
    return await pool.fetchval(
        f"""SELECT a.template_id FROM agents a
             JOIN users u ON u.actor_id = a.owner_actor_id
            WHERE u.uid = $1
              AND a.type = '{_DB_AGENT_TYPE}'
              AND a.status = 'active'
            ORDER BY a.created_at
            LIMIT 1""",
        user_uid,
    )


async def list_user_assistant_agents(pool, user_actor_id: uuid.UUID) -> list[dict]:
    """Return all active assistant agents for a user with admin template metadata."""
    from app.llm_models import label_for
    rows = await pool.fetch(
        f"""SELECT a.id AS agent_id,
                  a.name AS display_name,
                  a.model,
                  a.status,
                  a.created_at,
                  a.template_id,
                  admin_t.id   AS admin_template_id,
                  admin_t.name AS template_name,
                  admin_t.description AS template_description
             FROM agents a
             JOIN templates user_t  ON user_t.id  = a.template_id
             -- Resolve the admin template whether template_id points at a
             -- per-user template (parent_template_id set — legacy) or directly
             -- at the admin template itself (parent NULL — new model).
             LEFT JOIN templates admin_t ON admin_t.id = COALESCE(user_t.parent_template_id, user_t.id)
            WHERE a.owner_actor_id = $1
              AND a.type = '{_DB_AGENT_TYPE}'
              AND a.status = 'active'
            ORDER BY a.created_at""",
        user_actor_id,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["model_label"] = label_for(d.get("model") or "")
        out.append(d)
    logger.debug("list_user_assistant_agents: actor=%s count=%d", user_actor_id, len(out))
    return out


async def user_has_assistant_for_admin_template(
    pool, user_actor_id: uuid.UUID, admin_template_id: str
) -> bool:
    """Return True if user already has an active assistant from a given admin template."""
    result = await pool.fetchval(
        f"""SELECT 1 FROM agents a
             JOIN templates user_t ON user_t.id = a.template_id
            WHERE a.owner_actor_id = $1
              AND a.type = '{_DB_AGENT_TYPE}'
              AND a.status = 'active'
              -- Match whether the agent points at a per-user template whose
              -- parent is this admin template (legacy) or directly at this
              -- admin template (new model).
              AND COALESCE(user_t.parent_template_id, a.template_id) = $2
            LIMIT 1""",
        user_actor_id, admin_template_id,
    )
    return result is not None
