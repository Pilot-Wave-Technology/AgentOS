"""PostgreSQL connection pool — initialised once, shared across the app.

Uses asyncpg directly (no ORM).  Cloud SQL Auth Proxy provides the
connection when running on Cloud Run (Unix socket via --add-cloudsql-instances).
Firebase Admin SDK is used separately (in auth.py) only for JWT verification.
"""
import asyncio
import logging

import asyncpg

from app.config import get_settings

_pool: asyncpg.Pool | None = None
_log = logging.getLogger(__name__)


async def init_db() -> asyncpg.Pool:
    """Create the shared asyncpg connection pool.

    Retries up to 5 times with back-off to handle the Cloud Run race
    condition where the app starts before the Cloud SQL Auth Proxy
    sidecar is ready.
    """
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    last_err: Exception | None = None

    # Parse DSN — asyncpg's URL parser can choke on colons in Unix socket
    # paths (e.g. /cloudsql/project:region:instance), so we extract the
    # host parameter and pass it explicitly.
    dsn = settings.database_url.strip()
    connect_kwargs: dict = dict(min_size=2, max_size=20, command_timeout=30)

    if "?host=" in dsn:
        base, _, host_part = dsn.partition("?host=")
        connect_kwargs["dsn"] = base.strip()
        connect_kwargs["host"] = host_part.strip()
    else:
        connect_kwargs["dsn"] = dsn

    _log.info("Connecting to database (host=%s)", connect_kwargs.get("host", "from DSN"))

    # Short backoffs: Cloud SQL sidecar is typically ready within 200-500ms on
    # Cloud Run; the old `attempt * 2` schedule wasted 2s on the first miss.
    _BACKOFFS = [0.2, 0.5, 1.0, 2.0, 4.0]
    for attempt in range(1, 6):
        try:
            _pool = await asyncpg.create_pool(**connect_kwargs)
            _log.info("Database pool created on attempt %d", attempt)
            return _pool
        except (ConnectionRefusedError, OSError) as exc:
            last_err = exc
            wait = _BACKOFFS[attempt - 1]
            _log.warning(
                "DB connection attempt %d/5 failed (%s), retrying in %.1fs…",
                attempt, exc, wait,
            )
            await asyncio.sleep(wait)

    raise RuntimeError(f"Could not connect to database after 5 attempts: {last_err}")


def get_pool() -> asyncpg.Pool:
    """Return the running pool.  Raises if init_db() hasn't been called."""
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_db() first.")
    return _pool


async def resolve_goal_id(goal_id: str) -> str:
    """Look up the canonical goal ID case-insensitively.

    Gateway-side tools (resource_list, story_list, …) derive the
    goal_id from the agent's sessionKey. The gateway canonicalises every
    sessionKey to lowercase before tools see it, so we may receive
    ``bmiwp`` when the database stores ``BMIWP``. New goal IDs are 3–5
    uppercase alphanumerics; legacy IDs are 8-char lowercase hex — the
    two ranges don't overlap, so a case-insensitive lookup is
    unambiguous.

    Returns the stored form if a row exists, otherwise the input
    unchanged (so callers can still return the usual 404 / empty result
    paths rather than raising from here).
    """
    if not goal_id:
        return goal_id
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id FROM goals WHERE LOWER(id) = LOWER($1)", goal_id,
    )
    return row["id"] if row else goal_id


async def close_db():
    """Drain and close the pool.  Call at shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
