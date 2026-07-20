"""Valid status transitions for tools in the Tool Registry."""
import logging

logger = logging.getLogger(__name__)

# Admin-reachable transitions. Deliberately minimal — the meaningful states are
# enabled / disabled / deprecated (see research/tool_registry_cleanup_notes.md, Item 1):
#   - `deprecated` is SYSTEM-owned: only the catalog sweep sets it (raw UPDATE in
#     catalog_sync, which bypasses this map). Admins can recover from it (→ enabled) but
#     cannot SET it here — that keeps "deprecated = system removed this" unambiguous.
#   - `disabled` is admin-owned (sticky, never auto-restored).
# `beta` and `unavailable` are retired: they were behaviorally identical to `enabled` /
# `disabled` respectively and are no longer produced (existing rows migrated by
# retire_beta_unavailable_statuses.sql). They remain valid in tools_status_check only
# because that CHECK cannot shed values under the additive-migration rule.
TOOL_TRANSITIONS: dict[str, set[str]] = {
    "enabled":    {"disabled"},
    "disabled":   {"enabled"},
    "deprecated": {"enabled"},   # admin recovery only; the sweep is the sole setter of 'deprecated'
}

# Only these statuses are included in profile editors and resolver output. `beta` is kept
# in the active set (harmless — it is no longer produced) so the deployed
# tools_native_active_only CHECK, which references it, stays consistent.
TOOL_ACTIVE_STATUSES: frozenset[str] = frozenset({"enabled", "beta"})


def assert_tool_transition(current: str, target: str) -> None:
    """Raise ValueError if the current → target transition is not allowed."""
    allowed = TOOL_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(
            f"Invalid tool status transition: {current!r} → {target!r}. "
            f"Allowed from {current!r}: {sorted(allowed) or 'none'}"
        )
    logger.debug("Tool transition validated: %s → %s", current, target)


def is_code_level_native_tool(tool_row) -> bool:
    """True for an AgentOS-native tool hardcoded into the gateway (openclaw-tools.ts)
    and never reported by the tools.catalog RPC — identified by
    catalog_managed=false AND tier IS NULL.

    Their availability is governed by gateway code, not the registry, so the
    registry must never knock them out of an allowlist by moving them to a
    non-active status. This mirrors the DB-level tools_native_active_only CHECK
    constraint (migrations/lock_native_tool_active_status.sql) at the app layer,
    so callers get a clear ValueError instead of a raw asyncpg CheckViolationError.
    Accepts anything dict-like (asyncpg Record or plain dict)."""
    return tool_row.get("catalog_managed") is False and tool_row.get("tier") is None


def assert_native_tool_stays_active(tool_row, target_status: str) -> None:
    """Raise ValueError if a code-level native tool would leave the active set
    (enabled/beta). To retire such a tool, remove it from openclaw-tools.ts and
    ship a migration that flips catalog_managed=true, then deprecate it."""
    if is_code_level_native_tool(tool_row) and target_status not in TOOL_ACTIVE_STATUSES:
        name = tool_row.get("name", "?")
        raise ValueError(
            f"Tool {name!r} is a code-level native tool (hardcoded in the gateway, "
            f"not in the tool catalog); it may only be 'enabled' or 'beta', not "
            f"{target_status!r}. To retire it, remove it from openclaw-tools.ts and "
            f"ship a migration that sets catalog_managed=true before deprecating."
        )
    logger.debug(
        "Native-tool active-status guard passed: %s → %s",
        tool_row.get("name"), target_status,
    )
