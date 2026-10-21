"""The dashboard refresh loop: materialized views, and the gateway cache.

MOVED OUT OF `app/main.py` IN THE TAXIMETER WORK. It was started with a
bare `create_task` on every web instance, so every instance rebuilt
every view -- the work done N times, with N copies contending for the
same locks while doing it. It is registered in
`app/services/scheduler/registry.py` now, behind one leader, which is
that file's own test for whether a loop belongs there: running two of
these does the work twice rather than half each.

ONE REFRESH PATH. The admin Refresh button calls the same
`refresh_materialized_views()`, so there is no second way for a view to
be rebuilt and no second place for it to go wrong.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings


async def run_loop():
    """Refresh dashboard caches on two cadences from a single loop.

    Two different costs, two different freshness needs, one background task:
      • gateway_cache (health/readiness/presence/sessions/cron) — cheap and
        live-ish, refreshed EVERY tick (health interval, default 300s) so the
        gateway up/down strip stays current.
      • cost/fleet/SLO materialized views — expensive full rebuild, refreshed
        only every mat-view interval (default 3600s). refresh_materialized_views()
        rebuilds the views AND refreshes the gateway_cache in one call, so the
        mat-view tick covers both; the in-between ticks refresh only the cache.

    The admin Refresh button calls refresh_materialized_views() too, so there is
    a single refresh path — this loop just runs it on a schedule.

    The management service reads these from the DB; the main service owns the
    gateway connection so it populates the cache here. Waits 30s on first boot
    to let the gateway auth handshake complete, then does a full refresh on the
    first tick so the whole dashboard populates within ~30s of boot rather than
    after a full interval. Errors are non-fatal — stale data beats a crash loop.
    """
    _log = logging.getLogger("app.dashboard_refresh")
    settings = get_settings()
    health_interval = settings.dashboard_health_refresh_interval_seconds
    matview_interval = settings.dashboard_matview_refresh_interval_seconds
    # How many health ticks between full mat-view rebuilds (at least 1).
    ticks_per_matview = max(1, matview_interval // health_interval)
    _log.info(
        "dashboard refresh loop started (health=%ds, mat-views=%ds → every %d ticks)",
        health_interval, matview_interval, ticks_per_matview,
    )
    await asyncio.sleep(30)
    tick = 0
    while True:
        try:
            from app.services import dashboard_metrics
            from app.services.gateway_cache import refresh_gateway_cache
            # tick 0 (first after boot) and every Nth tick: full rebuild + cache.
            if tick % ticks_per_matview == 0:
                await dashboard_metrics.refresh_materialized_views()
            else:
                await refresh_gateway_cache()
        except Exception:
            _log.exception("dashboard refresh failed — non-fatal, will retry next cycle")
        tick += 1
        await asyncio.sleep(health_interval)
