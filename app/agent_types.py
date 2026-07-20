"""Single source of truth for agent type constants.

The database stores legacy type strings (e.g. 'SHCA') that predate the current
naming. This is the ONLY file in the codebase allowed to reference the raw DB
string 'SHCA'. All callers must pass DB values through normalize_agent_type()
before comparing or using them. SQL writes/queries use to_db_agent_type() to
convert back.

When the DB migration eventually runs to rename the CHECK constraint value from
'SHCA' to 'ASSISTANT', update _DB_TO_CODE and _CODE_TO_DB here — nothing else
needs to change.
"""
import logging

_log = logging.getLogger(__name__)

# Code-level constants — used everywhere in business logic, never the raw DB strings.
ASSISTANT = "ASSISTANT"
IA = "IA"
GO = "GO"
RA = "RA"
PLATFORM = "Platform"
PLATFORM_RESEARCH = "PlatformResearch"
# The Platform Improvement goal's executor assistant. A DEDICATED type (not SHCA)
# so it owns its own skill namespace (skills are pushed by type) and is excluded
# from the SHCA picker + cleanup reaper. Dispatches SHCA-shaped (is_group=False).
PLATFORM_ASSISTANT = "PlatformAssistant"

# DB → code mapping.
# actors.actor_type uses lowercase 'shca'; agents.type / agent_instances.type use 'SHCA'.
# Platform/PlatformResearch DB values match code values (mixed-case, no legacy alias).
_DB_TO_CODE: dict[str, str] = {
    "SHCA": ASSISTANT,
    "shca": ASSISTANT,
    "IA": IA,
    "GO": GO,
    "RA": RA,
    "Platform": PLATFORM,
    "PlatformResearch": PLATFORM_RESEARCH,
    "PlatformAssistant": PLATFORM_ASSISTANT,
}

# Code → DB mapping (for writes / WHERE clauses).
_CODE_TO_DB: dict[str, str] = {
    ASSISTANT: "SHCA",
    IA: "IA",
    GO: "GO",
    RA: "RA",
    PLATFORM: "Platform",
    PLATFORM_RESEARCH: "PlatformResearch",
    PLATFORM_ASSISTANT: "PlatformAssistant",
}


def normalize_agent_type(db_type: str | None) -> str | None:
    """Translate a raw DB agent type string to a code-level constant.

    Call this immediately after reading agent['type'] or actor_type from the DB.
    Never compare a raw DB value against the ASSISTANT constant directly.
    Returns None unchanged so callers don't need to guard against None.
    """
    if db_type is None:
        return None
    normalized = _DB_TO_CODE.get(db_type)
    if normalized is None:
        _log.debug("Unknown agent type from DB: %r — passing through unchanged", db_type)
        return db_type
    return normalized


def to_db_agent_type(code_type: str) -> str:
    """Translate a code-level constant to the DB value for writes / WHERE clauses."""
    return _CODE_TO_DB.get(code_type, code_type)
