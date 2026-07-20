"""CRUD operations for tool profiles and template-profile assignments.

A profile is a named bundle of tools. Profiles are assigned to agent templates
via the template_tool_profiles join table. At agent sync time, the assigned
profiles expand to a flat tools.allow list (see resolver.py).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def create_profile(
    pool,
    profile_id: str,
    name: str,
    description: str = "",
    tool_names: list[str] | None = None,
) -> dict:
    """Create a profile and populate its tool list atomically."""
    tool_names = tool_names or []
    existing = await pool.fetchval("SELECT 1 FROM tool_profiles WHERE id = $1", profile_id)
    if existing:
        raise ValueError(f"Profile '{profile_id}' already exists")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO tool_profiles (id, name, description) VALUES ($1, $2, $3)",
                profile_id, name, description,
            )
            for tool_name in tool_names:
                await conn.execute(
                    "INSERT INTO tool_profile_tools (profile_id, tool_name) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    profile_id, tool_name,
                )

    logger.info("Tool profile created: id=%s name=%r tools=%d", profile_id, name, len(tool_names))
    return await get_profile(pool, profile_id)


async def get_profile(pool, profile_id: str) -> Optional[dict]:
    """Return a profile with its tool_names list, or None if not found."""
    row = await pool.fetchrow("SELECT * FROM tool_profiles WHERE id = $1", profile_id)
    if not row:
        return None
    tool_rows = await pool.fetch(
        "SELECT tool_name FROM tool_profile_tools WHERE profile_id = $1 ORDER BY tool_name",
        profile_id,
    )
    result = dict(row)
    result["tool_names"] = [r["tool_name"] for r in tool_rows]
    return result


async def list_profiles(pool, status: str = "active") -> list[dict]:
    """Return all profiles with their tool_names populated, ordered by id."""
    rows = await pool.fetch(
        "SELECT * FROM tool_profiles WHERE status = $1 ORDER BY id", status,
    )
    profiles = []
    for row in rows:
        d = dict(row)
        tool_rows = await pool.fetch(
            "SELECT tool_name FROM tool_profile_tools WHERE profile_id = $1 ORDER BY tool_name",
            d["id"],
        )
        d["tool_names"] = [r["tool_name"] for r in tool_rows]
        profiles.append(d)
    logger.debug("Listed %d profiles (status=%s)", len(profiles), status)
    return profiles


async def update_profile(
    pool,
    profile_id: str,
    name: str | None = None,
    description: str | None = None,
    tool_names: list[str] | None = None,
) -> dict:
    """Update profile metadata and/or replace its full tool list atomically.

    If tool_names is None, the existing tool list is unchanged.
    If tool_names is an empty list, all tools are removed from the profile.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM tool_profiles WHERE id = $1 FOR UPDATE", profile_id,
            )
            if not row:
                raise ValueError(f"Profile '{profile_id}' not found")

            set_clauses: list[str] = []
            values: list = [profile_id]
            if name is not None:
                values.append(name)
                set_clauses.append(f"name = ${len(values)}")
            if description is not None:
                values.append(description)
                set_clauses.append(f"description = ${len(values)}")
            if set_clauses:
                set_clauses.append("updated_at = NOW()")
                await conn.execute(
                    f"UPDATE tool_profiles SET {', '.join(set_clauses)} WHERE id = $1",
                    *values,
                )

            if tool_names is not None:
                # Full replace: delete all existing, re-insert new list
                await conn.execute(
                    "DELETE FROM tool_profile_tools WHERE profile_id = $1", profile_id,
                )
                for tool_name in tool_names:
                    await conn.execute(
                        "INSERT INTO tool_profile_tools (profile_id, tool_name) "
                        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        profile_id, tool_name,
                    )

    logger.info(
        "Tool profile updated: id=%s name=%r tool_count=%s",
        profile_id, name, len(tool_names) if tool_names is not None else "unchanged",
    )
    return await get_profile(pool, profile_id)


async def deprecate_profile(pool, profile_id: str) -> dict:
    """Mark a profile as deprecated. Existing template assignments are preserved."""
    row = await pool.fetchrow(
        "UPDATE tool_profiles SET status = 'deprecated', updated_at = NOW() "
        "WHERE id = $1 RETURNING *",
        profile_id,
    )
    if not row:
        raise ValueError(f"Profile '{profile_id}' not found")
    logger.info("Tool profile deprecated: id=%s", profile_id)
    return await get_profile(pool, profile_id)


async def get_profile_templates(pool, profile_id: str) -> list[str]:
    """Return template IDs that have this profile assigned, ordered by template_id."""
    rows = await pool.fetch(
        "SELECT template_id FROM template_tool_profiles WHERE profile_id = $1 ORDER BY template_id",
        profile_id,
    )
    template_ids = [r["template_id"] for r in rows]
    logger.debug("Profile %s is assigned to %d templates: %s", profile_id, len(template_ids), template_ids)
    return template_ids


async def set_profile_templates(
    pool,
    profile_id: str,
    template_ids: list[str],
) -> None:
    """Update which templates use this profile.

    Only touches rows for this profile — other profiles on those templates are unaffected.
    New assignments get position = (current max position for that template) + 1 so they
    append to the end of an existing profile list without disrupting existing order.
    """
    new_set = set(template_ids)
    async with pool.acquire() as conn:
        async with conn.transaction():
            current_rows = await conn.fetch(
                "SELECT template_id FROM template_tool_profiles WHERE profile_id = $1",
                profile_id,
            )
            current_set = {r["template_id"] for r in current_rows}

            # Remove from templates no longer in the new list
            to_remove = current_set - new_set
            if to_remove:
                await conn.execute(
                    "DELETE FROM template_tool_profiles WHERE profile_id = $1 AND template_id = ANY($2::text[])",
                    profile_id, list(to_remove),
                )

            # Add to templates not yet in the current list
            to_add = new_set - current_set
            for template_id in to_add:
                # Append after existing profiles on that template
                max_pos = await conn.fetchval(
                    "SELECT COALESCE(MAX(position) + 1, 0) FROM template_tool_profiles WHERE template_id = $1",
                    template_id,
                )
                await conn.execute(
                    "INSERT INTO template_tool_profiles (template_id, profile_id, position) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    template_id, profile_id, max_pos,
                )

    logger.info(
        "Profile %s template assignments updated: added=%s removed=%s",
        profile_id, sorted(new_set - current_set), sorted(current_set - new_set),
    )


async def get_template_profiles(pool, template_id: str) -> list[str]:
    """Return profile IDs assigned to a template, ordered by position (lowest first)."""
    rows = await pool.fetch(
        "SELECT profile_id FROM template_tool_profiles "
        "WHERE template_id = $1 ORDER BY position ASC",
        template_id,
    )
    profile_ids = [r["profile_id"] for r in rows]
    logger.debug(
        "Fetched %d profiles for template %s: %s", len(profile_ids), template_id, profile_ids,
    )
    return profile_ids


async def set_template_profiles(
    pool,
    template_id: str,
    profile_ids: list[str],
) -> None:
    """Replace all profile assignments for a template atomically.

    Profiles are assigned with position values matching their index in profile_ids,
    so the first element resolves first during tool allowlist expansion.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM template_tool_profiles WHERE template_id = $1", template_id,
            )
            for position, profile_id in enumerate(profile_ids):
                await conn.execute(
                    "INSERT INTO template_tool_profiles (template_id, profile_id, position) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    template_id, profile_id, position,
                )
    logger.info(
        "Template profiles updated: template=%s profiles=%s", template_id, profile_ids,
    )
