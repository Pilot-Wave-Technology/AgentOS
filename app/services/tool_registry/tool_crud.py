"""CRUD operations for the tools catalog.

Tools are the atomic units of the registry — one row per tool name.
The name is the exact string passed to OpenClaw's tools.allow list.
"""
import logging
from typing import Optional

from app.services.transitions.tools import assert_native_tool_stays_active, assert_tool_transition

logger = logging.getLogger(__name__)


async def create_tool(
    pool,
    name: str,
    label: str = "",
    description: str = "",
    group_name: str = "",
    tags: list[str] | None = None,
    source: str = "core",
) -> dict:
    """Insert a new tool into the catalog. Raises ValueError if name already exists.

    This path creates NATIVE tools only (tier IS NULL) — custom tools go through the
    Custom Tools wizard (create_custom_tool). source is validated against the native set
    ('core' / 'platform') up front: 'custom' would violate tools_source_tier_consistency
    (source='custom' needs a tier) and any other value would violate tools_source_check.
    Rejecting here gives a clean, accurate error instead of a raw CheckViolationError.
    """
    tags = tags or []
    _ALLOWED_NATIVE_SOURCES = ("core", "platform")
    if source not in _ALLOWED_NATIVE_SOURCES:
        raise ValueError(
            f"Native tools must have source 'core' or 'platform', not {source!r}. "
            "(Custom tools are created through the Custom Tools wizard.)"
        )
    existing = await pool.fetchval("SELECT 1 FROM tools WHERE name = $1", name)
    if existing:
        raise ValueError(f"Tool '{name}' already exists in the catalog")

    # catalog_managed=false: a manually-registered tool is treated as code-level /
    # native (protected from the catalog-sync sweep, and non-deprecatable) until the
    # sync proves otherwise by sighting it in the tools.catalog RPC. Made explicit
    # here rather than relying on the column default, which is now also false.
    row = await pool.fetchrow(
        """INSERT INTO tools (name, label, description, group_name, tags, source, is_core, catalog_managed)
           VALUES ($1, $2, $3, $4, $5, $6, true, false)
           RETURNING *""",
        name, label, description, group_name, tags, source,
    )
    logger.info("Tool registered: name=%s group=%s source=%s", name, group_name, source)
    return dict(row)


async def get_tool(pool, name: str) -> Optional[dict]:
    """Return a single tool by name, or None if not found."""
    row = await pool.fetchrow("SELECT * FROM tools WHERE name = $1", name)
    return dict(row) if row else None


async def list_tools(
    pool,
    group_name: str = "",
    status: str = "enabled",
) -> list[dict]:
    """Return tools ordered by group then name, optionally filtered by group and status."""
    if group_name:
        rows = await pool.fetch(
            "SELECT * FROM tools WHERE group_name = $1 AND status = $2 ORDER BY name",
            group_name, status,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM tools WHERE status = $1 ORDER BY group_name, name",
            status,
        )
    logger.debug("Listed %d tools (group=%r status=%s)", len(rows), group_name, status)
    return [dict(r) for r in rows]


async def update_tool(pool, name: str, **kwargs) -> dict:
    """Partial update on a tool. Accepted kwargs: label, description, group_name, tags, source."""
    allowed_fields = {"label", "description", "group_name", "tags", "source"}
    fields = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}

    if not fields:
        row = await pool.fetchrow("SELECT * FROM tools WHERE name = $1", name)
        if not row:
            raise ValueError(f"Tool '{name}' not found")
        return dict(row)

    set_clauses = [f"{f} = ${i + 2}" for i, f in enumerate(fields)]
    set_clauses.append("updated_at = NOW()")
    sql = f"UPDATE tools SET {', '.join(set_clauses)} WHERE name = $1 RETURNING *"
    row = await pool.fetchrow(sql, name, *fields.values())
    if not row:
        raise ValueError(f"Tool '{name}' not found")

    logger.info("Tool updated: name=%s fields=%s", name, list(fields.keys()))
    return dict(row)


async def transition_tool(pool, name: str, target_status: str) -> dict:
    """Transition a tool to a new lifecycle status.

    Validates the transition against TOOL_TRANSITIONS before writing.
    Raises ValueError for unknown tools or invalid transitions.
    """
    row = await pool.fetchrow("SELECT * FROM tools WHERE name = $1", name)
    if not row:
        raise ValueError(f"Tool '{name}' not found")

    assert_tool_transition(row["status"], target_status)
    # Code-level native tools may only ever be active — refuse to knock one out of
    # agent allowlists (mirrors the tools_native_active_only DB constraint with a
    # clean error instead of a raw CheckViolationError).
    assert_native_tool_stays_active(row, target_status)

    updated = await pool.fetchrow(
        "UPDATE tools SET status = $2, updated_at = NOW() WHERE name = $1 RETURNING *",
        name, target_status,
    )
    logger.info("Tool transitioned: name=%s %s → %s", name, row["status"], target_status)
    return dict(updated)

# NOTE: there is intentionally no deprecate_tool() helper. 'deprecated' is a system-owned
# status set ONLY by the catalog sweep (catalog_sync._deprecate_missing, a raw UPDATE) —
# admins/services cannot transition a tool to 'deprecated' through the registry (it is not
# an admin-reachable target in TOOL_TRANSITIONS). See app/services/transitions/tools.py.
