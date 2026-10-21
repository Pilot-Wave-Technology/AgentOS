"""Platform-wide operator switches, read and written by the control panel.

THE VALUE IS A ROW; THE HISTORY IS THE LEDGER. `platform_settings`
answers "what is true now, and who last said so", which is what a
settings page has to render. Every change is also appended to the
hash-chained journal as an `execution.*` event, which is where every
other operator action already goes and which cannot be quietly
rewritten. Splitting them is deliberate: a `updated_by` column is a
convenience, not an audit trail, and an audit trail that lives in a
mutable column is not one.

WHY A ROW AND NOT AN ENVIRONMENT VARIABLE. Four capabilities in this
system were switched off by variables nobody had set, and not one of
them said so -- the assessor and three other LLM features behind
`AGENTOS_ASSIST`, the acceptance gate behind `AGENTOS_HUMAN_DONE`, the
build stamp on every log line, and three reapers behind an attribute no
driver defines. An env var cannot be read back by the product, cannot be
changed without a redeploy, and leaves no trace of who changed it.
"""
from __future__ import annotations

import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

VERIFICATION = "verification_enabled"

# HOW OFTEN GO CHECKS IN on a running autonomous errand, in seconds. A
# NUMBER, not a switch, which is why `platform_settings.value_int`
# exists -- see `migrations/errand_checkin_interval.sql`.
CHECKIN_SECONDS = "errand_checkin_seconds"

# What each switch means, for the settings page and for the audit event.
LABELS = {
    VERIFICATION: "Verification",
    CHECKIN_SECONDS: "Errand check-in interval",
}


async def get(key: str) -> dict:
    """`{key, value, updated_at, updated_by}`. Absent reads as off.

    An absent row and a row saying false are the same answer, and the
    migration seeds the row so they stay the same answer -- but a reader
    that assumed the row exists would be a reader that raises on a fresh
    database.
    """
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT key, value, updated_at, updated_by "
        "FROM platform_settings WHERE key = $1", key)
    if row is None:
        return {"key": key, "value": False, "updated_at": None,
                "updated_by": ""}
    return dict(row)


async def set_value(key: str, value: bool, *, by: str, reason: str = "") -> dict:
    """Write the switch and return `{was, now}`.

    RETURNS THE PRIOR VALUE, because the audit event needs it and
    reading it separately would be a race with the next admin.
    """
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        was = await conn.fetchval(
            "SELECT value FROM platform_settings WHERE key = $1 FOR UPDATE",
            key)
        await conn.execute(
            "INSERT INTO platform_settings (key, value, updated_by, "
            "updated_at) VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_by = EXCLUDED.updated_by, updated_at = now()",
            key, bool(value), by or "")
    _log.info("platform setting %s: %s -> %s by %s (reason=%s)",
              key, was, bool(value), by or "<unknown>", reason or "<none>")
    return {"was": bool(was) if was is not None else False,
            "now": bool(value)}


# ── numeric settings ─────────────────────────────────────────────────
#
# SEPARATE FUNCTIONS, NOT A POLYMORPHIC `get`. A caller that asked for a
# switch and got an integer would read 900 as true, and every one of
# these callers is a guard. The column a key uses is a property of the
# key, and reading the wrong one should be a name that does not exist
# rather than a value that quietly means the opposite.


async def get_int(key: str, default: int) -> int:
    """The numeric setting, or `default` when unset or not a number.

    FALLS BACK RATHER THAN RAISES, because every caller is on a loop
    that must keep running: a settings row somebody blanked should slow
    a feature back to its default, not stop the sweep that reads it.
    """
    pool = get_pool()
    try:
        raw = await pool.fetchval(
            "SELECT value_int FROM platform_settings WHERE key = $1", key)
    except Exception:                                       # noqa: BLE001
        _log.exception("platform setting %s: read failed, using %s",
                       key, default)
        return default
    return default if raw is None else int(raw)


async def set_int(key: str, value: int, *, by: str,
                  reason: str = "") -> dict:
    """Write the number and return `{was, now}`."""
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        was = await conn.fetchval(
            "SELECT value_int FROM platform_settings WHERE key = $1 "
            "FOR UPDATE", key)
        await conn.execute(
            "INSERT INTO platform_settings (key, value, value_int, "
            "updated_by, updated_at) VALUES ($1, NULL, $2, $3, now()) "
            "ON CONFLICT (key) DO UPDATE SET value_int = EXCLUDED.value_int, "
            "updated_by = EXCLUDED.updated_by, updated_at = now()",
            key, int(value), by or "")
    _log.info("platform setting %s: %s -> %s by %s (reason=%s)",
              key, was, int(value), by or "<unknown>", reason or "<none>")
    return {"was": was, "now": int(value)}
