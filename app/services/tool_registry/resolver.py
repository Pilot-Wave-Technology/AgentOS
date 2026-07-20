"""Tool allowlist resolver.

Expands a list of profile IDs and direct tool names into the single flat list
that gets passed to OpenClaw's agents.update({ tools: { allow: [...], deny: [...] } }).

This is the only module in the tool_registry package that touches the
OpenClaw boundary indirectly — its output is what agent_service.py sends to
gateway_admin.set_agent_tools(). No gateway calls happen here.
"""
import logging

from app.services.transitions.tools import TOOL_ACTIVE_STATUSES

logger = logging.getLogger(__name__)


async def resolve_tool_allowlist(
    pool,
    profiles: list[str],
    direct_tools: list[str],
    deny_tools: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Expand profile IDs + direct tool names into final allow and deny lists.

    Resolution order for the allow list:
      1. Each profile in `profiles` (in declaration order) contributes its tools.
      2. `direct_tools` are appended after all profile tools (highest specificity).
      3. First occurrence wins — duplicates across profiles or direct list are dropped.

    Cancellation (exact-match only):
      - Any tool name that appears in both the resolved allow list AND `deny_tools`
        is removed from BOTH lists. The tool is neither allowed nor denied in the
        final output — it is simply absent.
      - Only exact string equality is checked, no wildcards or group prefixes.

    Tools in a profile that are not in TOOL_ACTIVE_STATUSES (disabled, deprecated,
    unavailable) are silently excluded — the profile still resolves, just without them.

    Missing profiles are skipped with a warning and do NOT raise,
    so a stale profile assignment on a template will not block an agent sync.

    Args:
        pool:         asyncpg pool
        profiles:     ordered list of tool_profile ids (from template_tool_profiles)
        direct_tools: raw tool names (from templates.tools_allow or agents.tools_allow)
        deny_tools:   tool names to deny (template deny + instance deny, merged by caller)

    Returns:
        (allow, deny) — both lists are ready for
        gateway_admin.set_agent_tools(agent_id, allow, deny).
        An empty allow list means "all tools allowed" per OpenClaw's default.
    """
    result: list[str] = []
    seen: set[str] = set()

    if profiles:
        # Single JOIN query for all profiles at once — avoids N+1 fetches.
        # LEFT JOIN on tool_profile_tools so profiles with no tools still appear
        # (used to detect missing profile IDs). Inner JOIN on tools filters out
        # any tool not in TOOL_ACTIVE_STATUSES (disabled, deprecated, unavailable).
        active_statuses = list(TOOL_ACTIVE_STATUSES)
        profile_rows = await pool.fetch(
            """SELECT tp.id, tpt.tool_name
               FROM tool_profiles tp
               LEFT JOIN tool_profile_tools tpt ON tpt.profile_id = tp.id
               LEFT JOIN tools t ON t.name = tpt.tool_name
               WHERE tp.id = ANY($1::text[])
                 AND (tpt.tool_name IS NULL OR t.status = ANY($2::text[]))
               ORDER BY tp.id, tpt.tool_name""",
            profiles, active_statuses,
        )

        # Map profile_id → tool names; track which profile IDs were found in the DB.
        profile_map: dict[str, list[str]] = {pid: [] for pid in profiles}
        found_ids: set[str] = set()
        for row in profile_rows:
            found_ids.add(row["id"])
            if row["tool_name"]:  # LEFT JOIN may yield NULL tool_name for empty profiles
                profile_map[row["id"]].append(row["tool_name"])

        for pid in profiles:
            if pid not in found_ids:
                logger.warning(
                    "resolve_tool_allowlist: profile '%s' not found in DB, skipping", pid,
                )
                continue
            for tool_name in profile_map[pid]:
                if tool_name not in seen:
                    seen.add(tool_name)
                    result.append(tool_name)

    for tool_name in direct_tools:
        if tool_name not in seen:
            seen.add(tool_name)
            result.append(tool_name)

    # Exact-match cancellation: tools in both allow and deny are removed from both.
    effective_deny: list[str] = []
    if deny_tools:
        deny_set = set(deny_tools)
        allow_set = set(result)
        cancelled = allow_set & deny_set
        if cancelled:
            logger.info(
                "resolve_tool_allowlist: cancelled %d tool(s) present in both allow and deny: %s",
                len(cancelled), sorted(cancelled),
            )
        result = [t for t in result if t not in cancelled]
        effective_deny = [t for t in deny_tools if t not in cancelled]

    logger.debug(
        "resolve_tool_allowlist: profiles=%s direct=%d deny_in=%d → allow=%d deny=%d",
        profiles, len(direct_tools), len(deny_tools or []), len(result), len(effective_deny),
    )
    return result, effective_deny
