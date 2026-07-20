"""Management worker — standalone service for dashboard, settings, and admin.

Deployed as its own Cloud Run service (agent-management-{env}), VPC-internal
only, so all user traffic arrives via the main service's authenticated reverse
proxy. Direct public access is not possible.

This service:
  - opens its own DB pool (init_db)
  - serves dashboard, settings (including my-agents, model-pricing, memory prompts,
    signup-requests, integrations, claude-phases), notifications, and control-panel routes
  - runs the gateway_cache_refresh_loop so dashboard panel reads are instant
  - runs the Taximeter goal-weightage watchdog (app/services/taximeter/
    weightage_watchdog.py) — a lightweight periodic check, not goal-operational
    work; the actual recompute happens in the separate task-runner Cloud Run Job
  - does NOT register auth routes — login/signup/logout/refresh all live on the
    main service, which validates the firebase_token cookie before proxying
  - does NOT start task_controller, chat_module, council/security dispatchers,
    or any other goal-operational background tasks

Run: `uvicorn app.management_worker:app` (Cloud Build deploy step overrides
the image's default `app.main:app` entrypoint via --command/--args).
The /health route exists so Cloud Run has a port to probe.
"""
import asyncio
import logging
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import SignupRequired, SignupPending, SignupRejected
from app.config import get_settings
from app.roles import SYSTEM_ROLE_LABELS, GOAL_ROLE_LABELS
from app.database import init_db, close_db
from app.routers import admin, notifications
from app.routers import dashboard
from app.routers import control_panel
from app.routers import assistant
from app.routers import model_pricing as model_pricing_router
from app.memory_v2 import routers as memory_v2_routers

# Configure structured logging — mirrors app/main.py so Cloud Run maps severity.
import json as _json
import traceback as _traceback


class _CloudRunFormatter(logging.Formatter):
    _LEVEL_MAP = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": self._LEVEL_MAP.get(record.levelno, "DEFAULT"),
            "logger": record.name,
            "message": record.getMessage(),
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }
        if record.exc_info:
            payload["exception"] = "".join(_traceback.format_exception(*record.exc_info))
        return _json.dumps(payload)


_log_level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_CloudRunFormatter())
logging.basicConfig(handlers=[_handler], level=_log_level)

for _uvicorn_logger in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _uv_log = logging.getLogger(_uvicorn_logger)
    _uv_log.handlers = [_handler]
    _uv_log.propagate = False

app = FastAPI(title="Management Service", docs_url=None, redoc_url=None)

# Static files — same directory as main service (same Docker image)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers — no auth.router here: login/logout/refresh all live on the main
# service; management traffic always arrives via the main service proxy.
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(control_panel.router)
app.include_router(notifications.router)
# Settings sub-pages that are proxied from the main service and must be served here.
app.include_router(assistant.router)        # /settings/my-agents (+ assistant picker)
app.include_router(model_pricing_router.router)   # /settings/model-pricing
app.include_router(memory_v2_routers.router)      # /settings/memory/prompts

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["SYSTEM_ROLE_LABELS"] = SYSTEM_ROLE_LABELS
templates.env.globals["GOAL_ROLE_LABELS"] = GOAL_ROLE_LABELS

_proxy_log = logging.getLogger("app.management_worker.proxy")


@app.middleware("http")
async def log_proxied_request(request: Request, call_next):
    """Log every request arriving from the main service proxy.

    All traffic to this service arrives via the main service proxy — there is
    no public ingress. These logs make the handoff visible at both ends.
    """
    _start = time.monotonic()
    _is_htmx = bool(request.headers.get("hx-request"))
    _req_type = "htmx" if _is_htmx else "full-page"
    _forwarded_for = request.headers.get("x-forwarded-for", "—")

    _proxy_log.info(
        "MGMT ← MAIN  %s %s  [%s]  forwarded-for=%s",
        request.method, request.url.path, _req_type, _forwarded_for,
    )

    response = await call_next(request)

    _elapsed_ms = (time.monotonic() - _start) * 1000
    _proxy_log.info(
        "MGMT → MAIN  %s %s  %d  %.0fms",
        request.method, request.url.path, response.status_code, _elapsed_ms,
    )
    return response


@app.middleware("http")
async def inject_firebase_config(request: Request, call_next):
    """Make Firebase config available to all templates via request.state."""
    settings = get_settings()
    request.state.firebase_api_key = settings.firebase_web_api_key or ""
    request.state.firebase_project_id = settings.firebase_project_id or ""
    return await call_next(request)


@app.on_event("startup")
async def on_startup():
    await init_db()
    from app.services.taximeter.weightage_watchdog import weightage_watchdog_loop
    asyncio.create_task(weightage_watchdog_loop())
    logging.getLogger("app.management_worker").info("Management service started")


@app.on_event("shutdown")
async def on_shutdown():
    await close_db()


@app.get("/health")
async def health():
    return {"ok": True, "service": "management"}


@app.get("/")
async def root(request: Request):
    """Redirect root to dashboard (or login if unauthenticated)."""
    token = request.cookies.get("firebase_token")
    if not token:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")


# ── Signup gate handlers (copied from main.py) ───────────────────────────────

@app.exception_handler(SignupRequired)
async def signup_required_handler(request: Request, exc: SignupRequired):
    return RedirectResponse(url="/signup")


@app.exception_handler(SignupPending)
async def signup_pending_handler(request: Request, exc: SignupPending):
    return RedirectResponse(url="/signup/pending")


@app.exception_handler(SignupRejected)
async def signup_rejected_handler(request: Request, exc: SignupRejected):
    return RedirectResponse(url="/signup/rejected")


# ── HTTP error handlers (copied from main.py) ────────────────────────────────

@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc):
    logging.getLogger("app.management_worker").warning(
        "management: 401 on path=%s — redirecting to /login (cookie present=%s)",
        request.url.path,
        bool(request.cookies.get("firebase_token")),
    )
    return RedirectResponse(url="/login")


@app.exception_handler(400)
async def bad_request_handler(request: Request, exc):
    if "text/html" in request.headers.get("accept", ""):
        detail = getattr(exc, "detail", "Bad request")
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "code": 400, "message": str(detail), "user": None, "goals": []},
            status_code=400,
        )
    from fastapi.responses import JSONResponse as _JSONResponse
    return _JSONResponse({"detail": getattr(exc, "detail", "Bad request")}, status_code=400)


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "code": 403, "message": "You don't have permission to access this page.", "user": None, "goals": []},
        status_code=403,
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    path = request.url.path
    if path.startswith("/static") or path == "/favicon.ico":
        return PlainTextResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "code": 404, "message": "Page not found.", "user": None, "goals": []},
        status_code=404,
    )
