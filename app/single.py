"""Single-service entrypoint — the whole platform in one Cloud Run service.

`app.main` mounts every product router and, on startup, launches every
event-driven dispatcher (task_controller, chat, council, security, git
branch-ready listener, platform-council backstops — each flag-gated) plus the
always-on maintenance loops. It ALSO mounts `proxy_routes`, which in the SPLIT
profile forward the management surface (/dashboard, /settings, /admin,
/control-panel, /notifications) to a separate management service.

`app.main` deliberately does NOT start the loops that, in the split profile, live
only in the dedicated worker services:
  • council-worker: council_scheduler (12h sweep), security_scheduler, the
    Platform Research queue drainer (research_dispatcher);
  • git-engine-worker: git_graph_controller (the dependency-graph engine).
  • management tier: the taximeter weightage watchdog.

In the SINGLE profile there are no separate workers, so this module composes the
whole platform in ONE process:
  1. strips the proxy routes `app.main` registered (they'd shadow the real
     management routers below and every management page would hang / 404); and
  2. mounts the full management router set (mirrors `app.management_worker`); and
  3. starts the worker-tier loops app.main omits. Global single-run is
     DB-advisory-lock-enforced, so co-locating them is safe.

Used by terraform `service_profile = "single"` (the default). The `split` profile
instead runs `app.main` + `app.management_worker` + `app.council_worker` +
`app.git_engine_worker` as separate services.

    uvicorn app.single:app --host 0.0.0.0 --port 8080
"""
import asyncio
import logging

from app.main import app
from app import proxy_routes
from app.config import get_settings
from app.routers import admin, assistant, control_panel, dashboard, notifications
from app.routers import model_pricing as model_pricing_router
from app.memory_v2 import routers as memory_v2_routers

_log = logging.getLogger("app.single")

# 1. Drop the reverse-proxy routes app.main registered. In single profile the
#    management surface is served in-process, so proxying to a separate service
#    (which doesn't exist here) would just shadow the real routers below.
_proxy_endpoints = {getattr(r, "endpoint", None) for r in proxy_routes.router.routes}
app.router.routes = [
    r for r in app.router.routes if getattr(r, "endpoint", None) not in _proxy_endpoints
]

# 2. Mount the real management routers (mirrors app.management_worker).
for _r in (
    admin.router,
    dashboard.router,
    control_panel.router,          # /settings/tour-progress, etc.
    notifications.router,
    assistant.router,              # /settings/my-agents
    model_pricing_router.router,   # /settings/model-pricing
    memory_v2_routers.router,      # /settings/memory/prompts
):
    app.include_router(_r)


def _start(desc: str, fn) -> None:
    try:
        fn()
        _log.info("single-service: %s started", desc)
    except Exception as exc:  # non-fatal: keep the service up even if one loop fails
        _log.warning("single-service: %s NOT started: %s", desc, exc)


@app.on_event("startup")
async def _single_service_extras() -> None:
    """Start the loops that live in the split-profile worker services. FastAPI runs
    every registered startup handler, so app.main's startup (product routers +
    event-driven dispatchers + maintenance loops) still runs first."""
    settings = get_settings()

    # Management tier: taximeter weightage watchdog.
    def _weightage() -> None:
        from app.services.taximeter.weightage_watchdog import weightage_watchdog_loop
        asyncio.create_task(weightage_watchdog_loop())
    _start("weightage watchdog", _weightage)

    # Council tier (council-worker): 12h Goal Review Council scheduler. Self-gates
    # on COUNCIL_SCHEDULE_ENABLED; the event-driven dispatcher already runs in main.
    def _council_sched() -> None:
        from app.services.council.scheduler import council_scheduler
        council_scheduler.start()
    _start("council scheduler", _council_sched)

    # Security Council periodic sweep (council-worker). Self-gates on
    # SECURITY_SCHEDULE_ENABLED; dispatcher already runs in main.
    if settings.security_dispatcher_enabled:
        def _security_sched() -> None:
            from app.services.security.scheduler import security_scheduler
            security_scheduler.start()
        _start("security scheduler", _security_sched)

    # Platform Research queue drainer (council-worker) — drains platform_discoveries
    # one at a time. Flag-gated.
    if settings.platform_research_dispatcher_enabled:
        def _research() -> None:
            from app.services.platform_council.research_dispatcher import research_dispatcher
            research_dispatcher.start()
        _start("research dispatcher", _research)

    # Git dependency-graph engine (git-engine-worker) — leader election + git_dispatch
    # listener that cuts workstream branches and drives the merge queue. Flag-gated.
    if settings.gitgraph_enabled:
        def _git_engine() -> None:
            from app.services.git_graph.engine import git_graph_controller
            git_graph_controller.start()
        _start("git graph controller", _git_engine)
