"""Authenticated HTTP client for proxying requests to VPC-internal Cloud Run services.

A single persistent httpx.AsyncClient is shared for the process lifetime.
OIDC tokens are cached per-audience and refreshed 5 minutes before their
1-hour Cloud Run expiry, so every proxied request carries a valid Bearer
token without hitting the metadata server on every call.

Usage:
    # Startup
    proxy_client.create_client()

    # In a route handler
    client = proxy_client.get_client()
    token = await proxy_client.get_oidc_token(audience_url)

    # Shutdown
    await proxy_client.close_client()
"""
import asyncio
import logging
import time
from typing import Optional

import httpx

_log = logging.getLogger(__name__)

# Shared across the process — created at startup, closed at shutdown.
_client: Optional[httpx.AsyncClient] = None

# {audience_url: (token_string, expiry_monotonic_timestamp)}
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock: Optional[asyncio.Lock] = None

# Refresh 5 minutes before the 1-hour Cloud Run token expiry.
_TOKEN_TTL_SECONDS = 3300
_TOKEN_REFRESH_BUFFER_SECONDS = 300


def _get_lock() -> asyncio.Lock:
    """Return (or lazily create) the token lock for the running event loop."""
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    return _token_lock


def create_client() -> None:
    """Create the persistent proxy client. Must be called once at startup."""
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        # Never follow redirects — pass them back to the browser as-is.
        follow_redirects=False,
    )
    _log.info("Proxy HTTP client created")


async def close_client() -> None:
    """Close the persistent proxy client. Call at shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        _log.info("Proxy HTTP client closed")


def get_client() -> httpx.AsyncClient:
    """Return the shared proxy client. Raises RuntimeError if not initialized."""
    if _client is None:
        raise RuntimeError("Proxy client not initialized — call create_client() at startup")
    return _client


async def get_oidc_token(audience: str) -> str:
    """Return a valid OIDC token for the given Cloud Run service URL (the audience).

    Caches the token and refreshes it when fewer than 5 minutes remain before
    expiry. On environments without ADC (local dev), returns an empty string
    and logs a warning — the proxy still forwards the request so local testing
    works against a permissive management service.
    """
    lock = _get_lock()
    async with lock:
        now = time.monotonic()
        cached = _token_cache.get(audience)
        if cached and cached[1] - now > _TOKEN_REFRESH_BUFFER_SECONDS:
            _log.debug("Using cached OIDC token for audience=%s", audience)
            return cached[0]

        _log.info("Fetching fresh OIDC token for audience=%s", audience)
        try:
            loop = asyncio.get_event_loop()
            token = await loop.run_in_executor(None, _fetch_oidc_token_sync, audience)
            _token_cache[audience] = (token, now + _TOKEN_TTL_SECONDS)
            _log.info("OIDC token fetched and cached for audience=%s (ttl=%ds)", audience, _TOKEN_TTL_SECONDS)
            return token
        except Exception as exc:
            _log.warning(
                "OIDC token fetch failed for audience=%s — forwarding without Authorization header: %s",
                audience, exc,
            )
            return ""


def _fetch_oidc_token_sync(audience: str) -> str:
    """Fetch an OIDC token synchronously using google-auth ADC.

    Runs in a thread executor because google-auth's transport is synchronous.
    Cloud Run's metadata server returns a token valid for 1 hour.
    """
    import google.auth.transport.requests
    import google.oauth2.id_token
    auth_request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_request, audience)
