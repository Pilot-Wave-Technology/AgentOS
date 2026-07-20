"""Sync the OpenClaw tool catalog into the AgentOS tool registry.

Calling sync_openclaw_catalog() fetches the live catalog from the gateway
(tools.catalog RPC) and upserts every returned tool into the tools table.
Tools that previously existed in the registry but are no longer in the
catalog are transitioned to 'deprecated'.

Custom tools (tier IS NOT NULL) are never touched by this sync — they are
managed independently through the custom-tools admin UI.

AgentOS-native tools (source='platform') are hardcoded onto
agents in openclaw-tools.ts, not through listCoreToolSections() or a real
gateway plugin, so tools.catalog never reports them — their absence from the
catalog is meaningless, not a deletion. They are exempt from deprecation via
catalog_managed, on an inverted (safe-by-default) provenance model: the column
DEFAULTS to false ("not catalog-governed unless proven"), and this sync SETs it
true the instant it sights a tool in the catalog (see _upsert_catalog_tool). So
_deprecate_missing only ever touches tools the catalog is authoritative for, and
a new agent-hardcoded native tool is safe with no manual exemption. The DB
additionally enforces that a code-level native tool (tier IS NULL AND
catalog_managed=false) can only be 'enabled'/'beta' via the
tools_native_active_only CHECK, so even a stale revision running pre-fix code can
no longer deprecate one (see lock_native_tool_active_status.sql and
research/tool_deprecation_recurrence_root_cause.md). NOTE: the 'platform' source is
unrelated to the separate Platform Council subsystem (platform_registry,
platform-shca, PLATFORM_COUNCIL_ENABLED) — it's just a tools.source value.
"""
import logging
from typing import Any

log = logging.getLogger(__name__)


async def sync_openclaw_catalog(pool, gateway_admin) -> dict[str, int]:
    """Fetch the OpenClaw tool catalog and upsert into the tools table.

    Returns counts: { added, updated, deprecated, total }
    """
    log.info("Starting OpenClaw catalog sync")

    try:
        response = await gateway_admin.tools_catalog()
    except Exception as exc:
        log.warning("Catalog sync failed — could not fetch tools.catalog: %s", exc)
        return {"added": 0, "updated": 0, "deprecated": 0, "total": 0, "error": str(exc)}

    groups: list[dict] = response.get("groups") or []
    if not groups:
        log.warning("Catalog sync returned no groups — gateway may still be starting")
        return {"added": 0, "updated": 0, "deprecated": 0, "total": 0}

    # Flatten all entries from all groups into a single list
    catalog_entries = _flatten_catalog(groups)
    catalog_names = [e["name"] for e in catalog_entries]
    log.info("Catalog sync: %d tools across %d groups", len(catalog_entries), len(groups))

    added = updated = deprecated = 0

    for entry in catalog_entries:
        result = await _upsert_catalog_tool(pool, entry)
        if result == "added":
            added += 1
        elif result == "updated":
            updated += 1

    # Deprecate catalog-sourced tools that no longer appear in the catalog
    deprecated = await _deprecate_missing(pool, catalog_names)

    log.info(
        "Catalog sync complete: added=%d updated=%d deprecated=%d total=%d",
        added, updated, deprecated, len(catalog_entries),
    )
    return {
        "added": added,
        "updated": updated,
        "deprecated": deprecated,
        "total": len(catalog_entries),
    }


def _flatten_catalog(groups: list[dict]) -> list[dict[str, Any]]:
    """Convert the nested groups/tools structure into a flat list of tool entries.

    Skips the 'custom-tools' plugin group — those tools are AgentOS-managed
    custom tools reflected back by OpenClaw. Syncing them would re-insert them
    as catalog entries (tier IS NULL) and hide them from the custom tools UI.
    """
    entries = []
    for group in groups:
        plugin_id = group.get("pluginId")
        if plugin_id == "custom-tools":
            continue

        group_name = group.get("label") or group.get("id") or ""
        # We ship a customized OpenClaw fork, not a plugin host — every tool the catalog
        # reports is treated as built-in ('core'). The one real gateway extension
        # (custom-tools) is skipped above. 'platform' is reserved for AgentOS-native
        # tools, which are hardcoded onto agents and never appear in the catalog.
        source = "core"

        for tool in group.get("tools") or []:
            entries.append({
                "name": tool["id"],
                "label": tool.get("label") or tool["id"],
                "description": tool.get("description") or "",
                "source": source,
                "group_name": group_name,
            })
    return entries


async def _upsert_catalog_tool(pool, entry: dict[str, Any]) -> str:
    """Insert or update a single catalog tool. Returns 'added' or 'updated'."""
    existing = await pool.fetchrow(
        "SELECT name, label, description, source, group_name, tier, status, catalog_managed "
        "FROM tools WHERE name = $1",
        entry["name"],
    )

    if existing is None:
        # catalog_managed=true: this tool was just sighted in the tools.catalog RPC,
        # so the catalog IS authoritative for its liveness and _deprecate_missing may
        # deprecate it if it later disappears. This is the provenance signal the
        # inverted default (false) relies on — see lock_native_tool_active_status.sql.
        await pool.execute(
            """INSERT INTO tools (name, label, description, source, group_name, status, is_core, catalog_managed)
               VALUES ($1, $2, $3, $4, $5, 'enabled', true, true)""",
            entry["name"], entry["label"], entry["description"],
            entry["source"], entry["group_name"],
        )
        log.debug("Catalog sync added tool: %s (source=%s)", entry["name"], entry["source"])
        return "added"

    # Never overwrite a custom tool's execution columns
    if existing["tier"] is not None:
        log.debug("Catalog sync skipping custom tool: %s", entry["name"])
        return "skipped"

    changed = (
        existing["label"] != entry["label"]
        or existing["description"] != entry["description"]
        or existing["source"] != entry["source"]
        or existing["group_name"] != entry["group_name"]
        # Being sighted in the catalog IS the signal that the catalog governs this
        # tool — self-heal any row wrongly left catalog_managed=false (e.g. inserted
        # by a pre-fix revision during a deploy window, before the default flip).
        or existing["catalog_managed"] is not True
    )
    if changed:
        await pool.execute(
            """UPDATE tools
               SET label=$2, description=$3, source=$4, group_name=$5,
                   status = CASE WHEN status = 'deprecated' THEN 'enabled' ELSE status END,
                   catalog_managed = true,
                   updated_at = NOW()
               WHERE name=$1 AND tier IS NULL""",
            entry["name"], entry["label"], entry["description"],
            entry["source"], entry["group_name"],
        )
        log.debug("Catalog sync updated tool: %s", entry["name"])
        return "updated"

    # Re-enable a tool that was deprecated if it reappears in the catalog
    if existing.get("status") == "deprecated":
        await pool.execute(
            "UPDATE tools SET status='enabled', updated_at=NOW() WHERE name=$1 AND tier IS NULL",
            entry["name"],
        )
        return "updated"

    return "updated"  # exists and unchanged — still counts as seen


async def _deprecate_missing(pool, catalog_names: list[str]) -> int:
    """Mark catalog-sourced tools that no longer appear in the catalog as deprecated.

    Gated on catalog_managed=true, a data property (not a source-string match) —
    see add_catalog_managed_flag.sql for why: AgentOS-native tools registered
    directly on agents (see module docstring) are never reported by tools.catalog,
    so they'd otherwise get flagged deprecated on every single sync. Renaming a
    tool's `source` value never requires a matching code change here, which is the
    whole point — a prior source-string exclusion list caused a real deploy-race
    bug when a rename and this code disagreed for one sync cycle mid-cutover.
    """
    if not catalog_names:
        return 0

    result = await pool.fetch(
        """UPDATE tools
           SET status = 'deprecated', updated_at = NOW()
           WHERE catalog_managed
             AND tier IS NULL
             AND status != 'deprecated'
             AND name != ALL($1::text[])
           RETURNING name""",
        catalog_names,
    )
    count = len(result)
    if count:
        log.info(
            "Catalog sync deprecated %d tools no longer in catalog: %s",
            count, [r["name"] for r in result],
        )
    return count
