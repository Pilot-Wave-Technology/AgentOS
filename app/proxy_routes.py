"""Reverse proxy routes that forward management-plane requests to the
VPC-internal agent-management-{env} Cloud Run service.

Covered path prefixes (registered in main.py):
  /dashboard*
  /settings*
  /admin*      (management admin pages, NOT /admin/controls — that stays on main)
  /notifications*
  /control-panel*

Security model:
  - The user's firebase_token cookie is forwarded so the management service
    can validate the session independently.
  - An OIDC Bearer token is added so Cloud Run IAM allows the call once the
    management service is locked to --no-allow-unauthenticated.
  - Unauthenticated users (no valid cookie) are redirected to /login on the
    MAIN service — they never see the management service URL.

Header handling:
  - Hop-by-hop headers are stripped in both directions.
  - HTMX headers (HX-*) are forwarded both ways so HTMX fragments work.
  - X-Forwarded-* headers tell the management service the original host and
    path so Jinja2 url_for() and request.url.path produce correct values.
  - Cookie is forwarded so the management service sees the firebase_token.

All responses are streamed back to the browser (supports chunked HTMX lazy
panels and SSE without buffering the full body in memory).
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse

from app import proxy_client
from app.config import get_settings

_log = logging.getLogger(__name__)

router = APIRouter()

# Headers that must not be forwarded — they describe the connection, not the
# message, and re-sending them to httpx confuses the underlying transport.
_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})

# HTMX request headers to pass through to the management service.
_HTMX_REQUEST_HEADERS = frozenset({
    "hx-boosted",
    "hx-current-url",
    "hx-history-restore-request",
    "hx-prompt",
    "hx-request",
    "hx-swap",
    "hx-target",
    "hx-trigger",
    "hx-trigger-name",
})

# HTMX response headers to pass back to the browser.
_HTMX_RESPONSE_HEADERS = frozenset({
    "hx-location",
    "hx-push-url",
    "hx-redirect",
    "hx-refresh",
    "hx-replace-url",
    "hx-reselect",
    "hx-reswap",
    "hx-retarget",
    "hx-trigger",
    "hx-trigger-after-settle",
    "hx-trigger-after-swap",
})

# Standard response headers to copy back, beyond hop-by-hop exclusions.
# We pass everything except hop-by-hop and the ones httpx generates itself.
_EXCLUDED_RESPONSE_HEADERS = _HOP_BY_HOP | {"content-length", "content-encoding"}


def _is_authenticated(request: Request) -> bool:
    """Return True if the request carries a non-empty firebase_token cookie.

    This is a fast cookie presence check. The management service performs
    the full Firebase token verification for its own routes.
    """
    return bool(request.cookies.get("firebase_token"))


def _build_proxy_headers(request: Request, management_url: str) -> dict[str, str]:
    """Build the header set to forward to the management service."""
    headers: dict[str, str] = {}

    # Forward content-type and content-length for POST/PUT bodies.
    for name in ("content-type", "content-length", "accept", "accept-language"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    # Cookie — management service needs the firebase_token for auth.
    cookie = request.headers.get("cookie")
    if cookie:
        headers["cookie"] = cookie

    # X-Forwarded-* so Jinja2 templates and url_for() produce correct URLs.
    forwarded_for = request.headers.get("x-forwarded-for", "")
    client_ip = request.client.host if request.client else ""
    headers["x-forwarded-for"] = f"{forwarded_for}, {client_ip}".strip(", ") if forwarded_for else client_ip
    headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", "https")
    headers["x-forwarded-host"] = request.headers.get("host", "")
    # X-Forwarded-Prefix tells the management service its effective URL prefix
    # is empty (it serves all these routes at the same paths as the main service).
    headers["x-forwarded-prefix"] = ""

    # HTMX request headers.
    for name in _HTMX_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value

    return headers


async def _proxy(request: Request, path: str) -> StreamingResponse | RedirectResponse | HTMLResponse:
    """Core proxy handler — forwards the request to the management service and
    streams the response back.

    Args:
        request: The incoming FastAPI request.
        path: The full request path (e.g. "/dashboard/goals").

    Returns:
        A StreamingResponse if the management service responds, or an error
        page (502/503) if it is unreachable.
    """
    settings = get_settings()
    management_url = settings.management_service_url

    if not management_url:
        # Local dev: management service not configured — return helpful notice.
        _log.warning("Management service URL not configured; returning 503 for path=%s", path)
        return HTMLResponse(
            content=(
                "<h1>Management service not configured</h1>"
                "<p>Set MANAGEMENT_SERVICE_URL to the management service URL.</p>"
            ),
            status_code=503,
        )

    if not _is_authenticated(request):
        _log.info("Unauthenticated request to proxied path=%s — redirecting to /login", path)
        return RedirectResponse(url="/login", status_code=302)

    target_url = f"{management_url.rstrip('/')}{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    headers = _build_proxy_headers(request, management_url)

    # Attach OIDC Bearer token for Cloud Run IAM (no-op locally if ADC not set up).
    audience = management_url.rstrip("/")
    token = await proxy_client.get_oidc_token(audience)
    if token:
        headers["authorization"] = f"Bearer {token}"

    client = proxy_client.get_client()

    _is_htmx = bool(request.headers.get("hx-request"))
    _req_type = "htmx" if _is_htmx else "full-page"
    _start = time.monotonic()

    _log.info(
        "PROXY → MGMT  %s %s  [%s]",
        request.method, path, _req_type,
    )

    try:
        body = await request.body()
        upstream_response = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
    except Exception as exc:
        _elapsed_ms = (time.monotonic() - _start) * 1000
        _log.error(
            "PROXY → MGMT  %s %s  UNREACHABLE after %.0fms: %s",
            request.method, path, _elapsed_ms, exc,
        )
        return HTMLResponse(
            content=(
                "<h1>Management service unavailable</h1>"
                "<p>The management service could not be reached. "
                "Please try again in a moment.</p>"
            ),
            status_code=502,
        )

    _elapsed_ms = (time.monotonic() - _start) * 1000
    _log.info(
        "PROXY ← MGMT  %s %s  %d  %.0fms",
        request.method, path, upstream_response.status_code, _elapsed_ms,
    )

    # Build the response header set: copy safe upstream headers + HTMX headers.
    response_headers: dict[str, str] = {}
    for name, value in upstream_response.headers.items():
        lower = name.lower()
        if lower in _EXCLUDED_RESPONSE_HEADERS:
            continue
        response_headers[name] = value

    # Ensure HTMX response headers are explicitly included.
    for name in _HTMX_RESPONSE_HEADERS:
        value = upstream_response.headers.get(name)
        if value and name not in response_headers:
            response_headers[name] = value

    async def _stream():
        async for chunk in upstream_response.aiter_bytes(chunk_size=65536):
            yield chunk

    return StreamingResponse(
        content=_stream(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


# ── Route registrations ──────────────────────────────────────────────────────
# Each prefix captures both the root path and all sub-paths via {path:path}.
# The empty-path variant is needed because FastAPI doesn't match a trailing
# slash with {path:path} alone.

@router.api_route("/dashboard", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@router.api_route("/dashboard/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_dashboard(request: Request, path: str = ""):
    full_path = f"/dashboard/{path}".rstrip("/") if path else "/dashboard"
    return await _proxy(request, full_path)


@router.api_route("/settings", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@router.api_route("/settings/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_settings(request: Request, path: str = ""):
    full_path = f"/settings/{path}".rstrip("/") if path else "/settings"
    return await _proxy(request, full_path)


@router.api_route("/admin", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@router.api_route("/admin/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_admin(request: Request, path: str = ""):
    full_path = f"/admin/{path}".rstrip("/") if path else "/admin"
    # /admin/controls/* is served by admin_controls.router on the main service
    # and must NOT be proxied. FastAPI resolves the most-specific route first
    # (admin_controls.router uses explicit /admin/controls/{action} paths), so
    # this catch-all only fires for paths the main service doesn't own.
    return await _proxy(request, full_path)


@router.api_route("/notifications", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@router.api_route("/notifications/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_notifications(request: Request, path: str = ""):
    full_path = f"/notifications/{path}".rstrip("/") if path else "/notifications"
    return await _proxy(request, full_path)


@router.api_route("/control-panel", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@router.api_route("/control-panel/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_control_panel(request: Request, path: str = ""):
    full_path = f"/control-panel/{path}".rstrip("/") if path else "/control-panel"
    return await _proxy(request, full_path)
