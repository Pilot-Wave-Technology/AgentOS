"""Council worker — a standalone service that runs ONLY the Goal Review
Council dispatcher.

Deployed as its own Cloud Run service (council-worker-*) so council
provisioning + RA runs don't share the web tier's request budget. It:
  - opens its own DB pool (init_db) and its own gateway backend connection
    (gateway_admin self-connects on first use — the gateway tolerates multiple
    same-id 'gateway-client' backend connections, so this 3rd one is fine),
  - starts the council dispatcher, which LISTENs for `story_done` (final story
    reviews) and `council_wake` (manual runs / reimport enqueues) and processes
    the council_review_runs queue.

It deliberately does NOT mount the web routers. The web tier keeps the enqueue
endpoints (`/goals/{pid}/council/run`) and the RA submit endpoint
(`/internal/council/review`); this worker reads/writes the same queue tables, so
the split is purely about WHERE the dispatcher loop runs. Global single-run is
DB-enforced, so the web-tier dispatcher and this worker can both run during
cutover without double-processing.

Run: `uvicorn app.council_worker:app` (the Cloud Run deploy overrides the
image's default `app.main:app` command). The /health route exists only so Cloud
Run has a port to probe; the real work happens in the lifespan background tasks.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Configure logging BEFORE importing app modules so their INFO logs (DB pool,
# dispatcher, LISTEN) reach Cloud Run stdout — mirrors app/main.py. Without this
# the app loggers sit at WARNING and the worker runs silently.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

from app.config import get_settings
from app.database import init_db, close_db
from app.services.council.dispatcher import council_dispatcher
from app.services.council.scheduler import council_scheduler
from app.services.goal_summary.scheduler import goal_summary_scheduler

_log = logging.getLogger("app.council_worker")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    council_dispatcher.start()
    # Periodic full goal review for every active goal (default every 12h). The
    # event-driven story_done path and the manual run endpoint are unaffected.
    council_scheduler.start()
    # Goal progress summary scheduler — deterministic LLM doc generation per goal.
    if get_settings().goal_summary_enabled:
        goal_summary_scheduler.start()
    # Security Council dispatcher (P0) shares this worker — manual-trigger only,
    # flag-gated. It claims branch review runs (security_wake NOTIFY + poll),
    # runs the aspect + convener stages, and delivers cross-goal.
    security_on = get_settings().security_dispatcher_enabled
    if security_on:
        from app.services.security.dispatcher import security_dispatcher
        from app.services.security.scheduler import security_scheduler
        security_dispatcher.start()
        # Periodic security sweep over active branch resources — self-gates on
        # SECURITY_SCHEDULE_ENABLED (default OFF). Unchanged branches are skipped;
        # manual shield triggers always run.
        security_scheduler.start()
    # Research-queue drainer (Platform Research). Its OWN queue (platform_discoveries),
    # one-at-a-time end-to-end, parallel to the councils above. Flag-gated; supersedes
    # the daily deep-dive scheduler as the way discoveries get researched.
    research_on = get_settings().platform_research_dispatcher_enabled
    if research_on:
        from app.services.platform_council.research_dispatcher import research_dispatcher
        research_dispatcher.start()
    _log.info(
        "council-worker: started (council dispatcher + listeners + 12h scheduler%s%s)",
        " + security dispatcher + scheduler" if security_on else "",
        " + research dispatcher" if research_on else "",
    )
    try:
        yield
    finally:
        if research_on:
            try:
                from app.services.platform_council.research_dispatcher import research_dispatcher
                await research_dispatcher.stop()
            except Exception:
                _log.exception("council-worker: research dispatcher stop failed")
        if security_on:
            try:
                from app.services.security.scheduler import security_scheduler
                await security_scheduler.stop()
            except Exception:
                _log.exception("council-worker: security scheduler stop failed")
            try:
                from app.services.security.dispatcher import security_dispatcher
                await security_dispatcher.stop()
            except Exception:
                _log.exception("council-worker: security dispatcher stop failed")
        if get_settings().goal_summary_enabled:
            try:
                await goal_summary_scheduler.stop()
            except Exception:
                _log.exception("council-worker: goal-summary scheduler stop failed")
        try:
            await council_scheduler.stop()
        except Exception:
            _log.exception("council-worker: scheduler stop failed")
        try:
            await council_dispatcher.stop()
        except Exception:
            _log.exception("council-worker: dispatcher stop failed")
        try:
            await close_db()
        except Exception:
            pass


app = FastAPI(title="council-worker", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "service": "council-worker"}
