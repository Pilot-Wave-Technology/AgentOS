"""Resolves a `model_providers` row's Secret Manager reference to an actual
API key value, for direct-SDK-call utility purposes (see
`app/services/utility_llm.py`).

This is the agentos-side counterpart to the OpenClaw gateway's own
`extensions/model-registry/src/secret-resolver.ts` — that one resolves
credentials for agents deployed via the AgentOS UI, injected into the
gateway process's env. This one resolves the SAME `model_providers`
secret references, but for agentos's own direct utility calls
(summarization, template generation, …) — see
`documentation/model_registry_secret_management_design.md` for the
credential-reference design this reads from.

`secretmanager.SecretManagerServiceClient` has no async API — every call
here runs the blocking client in a thread via `asyncio.to_thread`. A short
TTL cache avoids a Secret Manager round trip on every utility call.
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 600.0
_cache: dict[str, tuple[str, float]] = {}


def _access_secret_version_sync(project: str, secret_id: str, version: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("utf-8")


async def resolve_provider_secret(
    secret_project: str | None,
    secret_id: str | None,
    secret_version: str | None,
) -> str:
    """Return the resolved secret value, or "" if no reference is
    configured or resolution fails. Never raises — a credential outage
    should degrade to "no key" (caller reports a clear error), not crash
    the utility call site."""
    if not secret_project or not secret_id:
        return ""

    version = secret_version or "latest"
    cache_key = f"{secret_project}/{secret_id}/{version}"
    cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]

    try:
        value = await asyncio.to_thread(
            _access_secret_version_sync, secret_project, secret_id, version
        )
    except Exception as exc:
        logger.warning(
            "secret_resolver: failed to resolve secret %s — %s", cache_key, exc
        )
        return ""

    _cache[cache_key] = (value, time.monotonic())
    logger.info("secret_resolver: resolved secret %s", cache_key)
    return value


def clear_cache() -> None:
    _cache.clear()
