"""Per-user explainability preferences: the (i) info-popover, hover cards,
startup wizard.

Backs Settings > Help & Explainability. A row is created lazily on first
write (upsert) — users who existed before this table shipped simply read
back the hardcoded defaults until they change something.

Column names are historical and no longer describe what they gate — kept
as-is rather than migrating the schema for a rename, since the DB columns
themselves aren't user-visible (only the Settings labels are, and those
are just the label strings in settings/preferences.html):
  - tooltips_enabled column -> Settings label "Tooltip" -> gates the (i)
    info-popover (click-triggered). See pwtTogglePopover() in app.js.
  - hover_help_enabled column -> Settings label "Hover cards" -> gates every
    hover-triggered explanation: the sidebar preview card AND every plain
    title="" hint in the app, which renders through that same hover-card
    mechanism instead of the browser's native tooltip. See initHoverCards()
    in base.html.
"""
import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

_DEFAULTS = {
    "tooltips_enabled": True,
    "hover_help_enabled": True,
    "startup_wizard_enabled": True,
}

# Allowlist used to safely interpolate a column name into the upsert below —
# `key` must never reach SQL construction without passing this check first.
_ALLOWED_KEYS = frozenset(_DEFAULTS.keys())


async def get_preferences(user_id: str) -> dict:
    """Returns the user's preference dict, falling back to defaults if no row exists yet."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT tooltips_enabled, hover_help_enabled, startup_wizard_enabled "
        "FROM user_preferences WHERE user_id = $1",
        user_id,
    )
    if row is None:
        _log.debug("user_preferences: no row for user_id=%s, using defaults", user_id)
        return dict(_DEFAULTS)
    return dict(row)


async def set_preference(user_id: str, key: str, value: bool) -> dict:
    """Upserts a single preference and returns the full updated preference dict."""
    if key not in _ALLOWED_KEYS:
        raise ValueError(f"Unknown preference key: {key}")
    pool = get_pool()
    await pool.execute(
        f"""INSERT INTO user_preferences (user_id, {key})
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET {key} = EXCLUDED.{key}, updated_at = now()""",
        user_id, value,
    )
    _log.info("user_preferences: user_id=%s set %s=%s", user_id, key, value)
    return await get_preferences(user_id)
