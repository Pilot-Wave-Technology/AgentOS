"""Shared, rate-limit-aware GitHub transport for the git engine (spec §7 / §0.1).

Replaces the per-call ``httpx.AsyncClient()`` pattern in github_ops (no pool, no
backoff, no budget → self-DoS risk under the engine's read load, which is shared
across every goal on one installation). Provides:

  - ONE pooled httpx client (lazy singleton; ``aclose()`` on worker shutdown),
  - a global concurrency cap (well under GitHub's 100-concurrent secondary ceiling),
  - REST and GraphQL transports,
  - SEPARATE REST and GraphQL rate budgets. GitHub meters them INDEPENDENTLY
    (REST 5,000 req/hr; GraphQL 5,000 points/hr per installation), so a single
    bucket would self-throttle one while exhausting the other. Each budget is
    refreshed from its own response's ``X-RateLimit-*`` headers; a call pre-sleeps
    when its bucket is exhausted,
  - bounded retry with backoff+jitter on transient failures (5xx, 429, and
    403+``Retry-After`` secondary-limit).

Token-stateless: the caller passes a freshly-minted installation token per repo
(``github_tokens``). This module owns only the connection pool + the throttle,
never auth. Callers keep their own status-code handling (verify-by-read) — ``rest``
returns the raw ``httpx.Response``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

import httpx

_log = logging.getLogger(__name__)

_API = "https://api.github.com"
_GRAPHQL = "https://api.github.com/graphql"
_TIMEOUT = 20.0
_MAX_RETRIES = 3
# Well below GitHub's 100-concurrent secondary limit (one installation, shared
# across all goals) — leaves headroom for agent-side GitHub traffic.
_CONCURRENCY = 8
_MAX_PRESLEEP = 300.0   # cap a single budget-exhaustion pre-sleep (5 min)
_MAX_RETRY_SLEEP = 60.0

_client: Optional[httpx.AsyncClient] = None
# Lock + semaphore are created LAZILY on the running loop (not at import) so they
# bind to the worker's event loop, not whatever loop happens to be alive at import
# (matters for tests / any multi-loop host). Creation is a synchronous block with no
# await, so it's atomic w.r.t. the single asyncio loop — no guard needed.
_client_lock: Optional[asyncio.Lock] = None
_sem: Optional[asyncio.Semaphore] = None


def _get_lock() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(_CONCURRENCY)
    return _sem


@dataclass
class _Budget:
    """One GitHub rate bucket (REST or GraphQL), tracked from X-RateLimit-* headers."""
    name: str
    remaining: int = 5000
    reset_at: float = 0.0          # epoch seconds (X-RateLimit-Reset)

    def update(self, headers: httpx.Headers) -> None:
        try:
            rem = headers.get("x-ratelimit-remaining")
            rst = headers.get("x-ratelimit-reset")
            if rem is not None:
                self.remaining = int(rem)
            if rst is not None:
                self.reset_at = float(rst)
        except (TypeError, ValueError):
            pass

    async def gate(self) -> None:
        """If this bucket is exhausted, sleep until its reset (capped)."""
        if self.remaining > 0:
            return
        wait = self.reset_at - time.time()
        if wait > 0:
            wait = min(wait, _MAX_PRESLEEP)
            _log.warning("github %s budget exhausted; sleeping %.0fs to reset",
                         self.name, wait)
            await asyncio.sleep(wait)


_rest_budget = _Budget("rest")
_graphql_budget = _Budget("graphql")


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _get_lock():
            if _client is None:
                _client = httpx.AsyncClient(
                    timeout=_TIMEOUT,
                    limits=httpx.Limits(
                        max_connections=_CONCURRENCY * 2,
                        max_keepalive_connections=_CONCURRENCY,
                    ),
                )
    return _client


async def aclose() -> None:
    """Close the pooled client (call on engine-worker shutdown)."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


def _headers(token: str, accept: Optional[str] = None) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept or "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _backoff(attempt: int) -> float:
    return min(2.0 ** attempt, 30.0) + random.uniform(0.0, 1.0)


def _is_secondary_limit(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or (
        resp.status_code == 403 and resp.headers.get("retry-after") is not None
    )


def _primary_exhausted(resp: httpx.Response) -> bool:
    return resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"


async def rest(
    method: str, path: str, token: str, *,
    params: Optional[dict] = None, json: Optional[dict] = None,
    accept: Optional[str] = None,
) -> httpx.Response:
    """One REST call through the pooled client, with budget gating + bounded retry.

    ``path`` is appended to the API root (e.g. ``/repos/o/n/pulls/5``). Returns the
    raw ``httpx.Response`` so callers keep their verify-by-read status handling.
    Only transient failures are retried (5xx, secondary-limit, transport errors);
    ordinary 4xx is returned as-is for the caller to classify.
    """
    client = await _get_client()
    hdrs = _headers(token, accept)
    url = path if path.startswith("http") else f"{_API}{path}"
    last_exc: Optional[Exception] = None
    resp: Optional[httpx.Response] = None
    for attempt in range(_MAX_RETRIES + 1):
        await _rest_budget.gate()
        async with _get_sem():
            try:
                resp = await client.request(method, url, headers=hdrs,
                                            params=params, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise
        _rest_budget.update(resp.headers)
        if attempt < _MAX_RETRIES:
            if _is_secondary_limit(resp):
                ra = resp.headers.get("retry-after")
                wait = float(ra) if (ra and ra.isdigit()) else _backoff(attempt)
                _log.warning("github secondary limit (%d) on %s %s; retry in %.0fs",
                             resp.status_code, method, path, min(wait, _MAX_RETRY_SLEEP))
                await asyncio.sleep(min(wait, _MAX_RETRY_SLEEP))
                continue
            if _primary_exhausted(resp):
                await _rest_budget.gate()
                # Floor sleep: if reset_at is already past (clock skew / boundary),
                # gate() returns instantly — avoid a tight spin against a 403.
                await asyncio.sleep(_backoff(attempt))
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(_backoff(attempt))
                continue
        return resp
    if last_exc:
        raise last_exc
    assert resp is not None
    return resp


async def graphql(query: str, variables: dict, token: str, *,
                  accept: Optional[str] = None) -> dict:
    """One GraphQL POST through the pooled client. Returns ``data``. Updates the
    SEPARATE GraphQL budget from the response's X-RateLimit-* headers.

    Errors handling is partial-result-aware: if the body carries ``data`` AND
    ``errors`` (GitHub's usual shape for a field-level/NOT_FOUND error with a
    null sub-field), we log and RETURN the data so the caller's null-handling
    applies; we only RAISE when there is no usable ``data``.

    ``accept`` overrides the Accept header — needed for schema-preview fields
    (e.g. ``mergeStateStatus`` → ``application/vnd.github.merge-info-preview+json``).
    """
    client = await _get_client()
    hdrs = _headers(token, accept)
    last_exc: Optional[Exception] = None
    resp: Optional[httpx.Response] = None
    for attempt in range(_MAX_RETRIES + 1):
        await _graphql_budget.gate()
        async with _get_sem():
            try:
                resp = await client.post(_GRAPHQL, headers=hdrs,
                                         json={"query": query, "variables": variables})
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise
        _graphql_budget.update(resp.headers)
        if attempt < _MAX_RETRIES:
            if _is_secondary_limit(resp):
                ra = resp.headers.get("retry-after")
                wait = float(ra) if (ra and ra.isdigit()) else _backoff(attempt)
                await asyncio.sleep(min(wait, _MAX_RETRY_SLEEP))
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(_backoff(attempt))
                continue
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data")
        if body.get("errors"):
            if data:  # partial success — log, let the caller's null-handling apply
                _log.warning("graphql partial errors: %s", body["errors"])
                return data
            raise RuntimeError(f"graphql errors: {body['errors']}")
        return data or {}
    if last_exc:
        raise last_exc
    raise RuntimeError("graphql: retries exhausted")
