"""Gateway response cache.

Stores the last successful result from each monitored gateway RPC/HTTP call
in the `gateway_cache` table so dashboard panels read from PostgreSQL instead
of calling the gateway on every page load.

The background loop in app/main.py calls refresh_gateway_cache() every 30s.
All dashboard reads go through read() which returns {} on a cold-cache miss —
callers handle that the same way they handle a gateway timeout.

Design note: the full gateway response dict is stored as JSONB so callers
receive identical structure to what gateway_admin.call() returns, making the
switch from live call → cache read a one-line change at each call site.

Health-specific wrapping: the 'health' cache key stores the raw RPC result
augmented with '_ok: True' on success or '_ok: False, error: ...' on failure.
This lets _system_health() determine gateway up/down from the cached dict
without needing to distinguish a fresh-empty cache from a known failure.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.database import get_pool
from app.openclaw import gateway_admin, gateway_health, gateway_readiness

_log = logging.getLogger(__name__)

# Background refresh timeout. Calls run sequentially so this is per-call,
# not the total refresh time. 8s matches the original live-call budget and
# gives enough headroom for gateway hiccups without the loop stalling for too
# long (worst case: 7 calls × 8s = 56s, rare, and the loop sleeps 30s after).
_GATEWAY_TIMEOUT = 8.0


async def read(cache_key: str) -> dict:
    """Return the cached gateway response for cache_key, or {} on miss.

    Never raises — callers treat {} the same as a gateway timeout.
    """
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT data FROM gateway_cache WHERE cache_key = $1", cache_key,
        )
        if row:
            data = row["data"]
            if isinstance(data, dict):
                return data
            if isinstance(data, str):
                return json.loads(data)
            return data if data else {}
        return {}
    except Exception as exc:
        _log.warning("gateway_cache.read key=%s failed: %s", cache_key, exc)
        return {}


async def _refresh_health() -> dict:
    """Refresh the 'health' key — wraps RPC result with _ok flag.

    Primary: WS RPC "health" — definitive proof the gateway connection works.
    Fallback: HTTP /health endpoint — covers the case where the "health" RPC
    method is slow/absent but the gateway itself is reachable (all other RPCs
    work). Mirrors the original live _system_health() fallback logic.

    Stores '_ok': True/False so callers can distinguish a known-down gateway
    from a cold-cache miss ({} has no _ok key).
    """
    try:
        result = await gateway_admin.call("health", {}, timeout=_GATEWAY_TIMEOUT)
        _log.debug("gateway_cache health RPC ok")
        return {**result, "_ok": True}
    except Exception as exc:
        _log.warning("gateway_cache health RPC failed, trying HTTP fallback: %s", exc)

    # HTTP /readyz fallback — gateway_readiness() returns {} on network failure
    # and a non-empty dict when the endpoint responds. Any non-empty response
    # proves the gateway process is alive and reachable.
    readiness = await gateway_readiness()
    if readiness:
        _log.debug("gateway_cache health: readyz fallback confirmed gateway up")
        return {**readiness, "_ok": True}

    # Last resort: HTTP /health — accept any response that isn't a plain
    # network failure (gateway_health returns {"status":"unreachable"} on exc).
    try:
        gw = await gateway_health()
        gateway_ok = gw.get("status") != "unreachable"
        _log.debug("gateway_cache health HTTP fallback ok=%s status=%s", gateway_ok, gw.get("status"))
        return {**gw, "_ok": gateway_ok}
    except Exception as exc2:
        _log.warning("gateway_cache health HTTP fallback also failed: %s", exc2)
        return {"_ok": False, "error": str(exc2)}


async def _refresh_readiness() -> dict:
    """Refresh the 'readiness' key via the HTTP /readyz endpoint."""
    return await gateway_readiness()


async def _refresh_presence() -> dict:
    """Refresh system-presence. Gateway returns a list; wrap in dict for uniform storage."""
    result = await gateway_admin.call("system-presence", {}, timeout=_GATEWAY_TIMEOUT)
    if isinstance(result, list):
        return {"items": result}
    return result if isinstance(result, dict) else {}


async def _refresh_rpc(method: str, params: dict) -> dict:
    """Refresh a plain RPC call — returns {} on failure (keeps old cache value)."""
    return await gateway_admin.call(method, params, timeout=_GATEWAY_TIMEOUT)


async def _write_cache(pool, cache_key: str, data: dict) -> None:
    """Upsert one gateway_cache row.

    Uses INSERT ... ON CONFLICT (not UPDATE) so a missing seed row self-heals
    instead of silently swallowing the write. A plain UPDATE ... WHERE cache_key
    is a zero-row no-op when the row was never seeded — which is exactly how
    health/readiness/system-presence went stale on DBs that applied gateway_cache
    before its seed list was extended (see migration gateway_cache_seed_health_keys).
    """
    await pool.execute(
        """
        INSERT INTO gateway_cache (cache_key, data, refreshed_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (cache_key) DO UPDATE
            SET data = EXCLUDED.data, refreshed_at = now()
        """,
        cache_key, json.dumps(data),
    )


async def refresh_gateway_cache() -> datetime:
    """Refresh all gateway cache keys sequentially and persist results.

    Calls are run ONE AT A TIME — the gateway has limited concurrency and
    fires all parallel requests into a queue, causing every call to timeout
    when they run simultaneously. Sequential execution gives each call an
    uncontested gateway window; total wall-clock is just the sum of call
    times (~1-2s) which is fine for a background task.

    Returns the refresh timestamp. On failure for a given key the previous
    cached value is retained (except 'health' which always writes to record
    the failure state so the panel can show degraded).
    """
    pool = get_pool()

    # Ordered list: (cache_key, coroutine_factory, write_on_failure)
    # Coroutines are created fresh per call so they aren't already awaited.
    calls: list[tuple[str, object, bool]] = [
        ("health",          lambda: _refresh_health(),                                          True),
        ("readiness",       lambda: _refresh_readiness(),                                       False),
        ("system-presence", lambda: _refresh_presence(),                                        False),
        ("sandbox.status",  lambda: _refresh_rpc("sandbox.status",  {}),                       False),
        ("sessions.list",   lambda: _refresh_rpc("sessions.list",   {"limit": 5, "includeLastMessage": True}), False),
        ("sessions.usage",  lambda: _refresh_rpc("sessions.usage",  {}),                       False),
        ("cron.list",       lambda: _refresh_rpc("cron.list",       {"limit": 20, "sortBy": "nextRunAtMs"}), False),
    ]

    for key, coro_factory, always_write in calls:
        try:
            result = await coro_factory()
            await _write_cache(pool, key, result)
            _log.debug("gateway_cache refreshed key=%s", key)
        except Exception as exc:
            if always_write:
                try:
                    await _write_cache(pool, key, {"_ok": False, "error": str(exc)})
                except Exception:
                    pass
            _log.warning("gateway_cache refresh key=%s failed: %s", key, exc)

    now = datetime.now(timezone.utc)
    _log.info("gateway_cache refresh done keys=%d", len(calls))
    return now


async def get_min_refreshed_at() -> datetime | None:
    """Return the oldest refreshed_at across all cached keys, or None on error."""
    try:
        pool = get_pool()
        val = await pool.fetchval("SELECT MIN(refreshed_at) FROM gateway_cache")
        return val
    except Exception as exc:
        _log.warning("gateway_cache.get_min_refreshed_at failed: %s", exc)
        return None
