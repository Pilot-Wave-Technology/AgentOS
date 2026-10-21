"""Gemini prompt caching — an explicit `cachedContents` resource.

WHY EXPLICIT. Gemini's implicit caching is best-effort and mostly does
not fire at our sizes. Measured on a live corpus run: `cacheRead`
populated on **14 of 190 steps**. A controlled probe with a byte-stable
growing prefix cached at 20K and 60K and never at 5K, 8K, 12K or 16K —
not a threshold, just best-effort. Our uncached steps average 7,682
input tokens, squarely in the band that never hits.

WHAT IS CACHED: the system instruction and the tool declarations. Not
the conversation. The conversation grows every step, and a Gemini cache
is a created, stored resource — re-creating it each step would cost more
than it saves. System-plus-tools is byte-identical on every step of
every leg of every run, which is exactly what a cache is for.

The shape follows upstream OpenClaw's `google-prompt-cache.ts`, which
solved this before us. Two of its details are not obvious and both are
load-bearing:

  * THE KEY IS A CONTENT DIGEST. A cache keyed on anything else silently
    serves the previous system prompt or the previous tool surface after
    a change — the same class of defect as the model default that was
    wrong for a whole stage.
  * TOOLS ARE SORTED BEFORE HASHING. The same surface in a different
    order is otherwise a different digest, and the cache never hits.

And one it has that this project has no equivalent of: a refused
creation is remembered with a BACKOFF. Without it a provider that cannot
cache is asked again on every step, paying for the failure repeatedly
and never saying so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)

# Process-level, keyed by (model, digest). Not persisted: a worker that
# restarts has lost nothing it cannot rebuild in one call, and a cache
# entry that outlives the process it was created for is a stale entry
# nobody is checking.
_ENTRIES: dict[tuple, dict] = {}


def tool_names(tools) -> list:
    """Names out of either shape the caller might hold.

    The adapter converts schemas to the SDK's `Tool` objects before it
    gets here, and a digest that only understood dicts crashed on the
    first real call. Handling both is one function; asking every caller
    to pass the right one is a rule somebody forgets.
    """
    names = []
    for tool in tools or []:
        if isinstance(tool, dict):
            names.append(str(tool.get("name", "")))
            continue
        declarations = getattr(tool, "function_declarations", None)
        if declarations:
            names.extend(str(getattr(d, "name", "")) for d in declarations)
        elif getattr(tool, "name", None):
            names.append(str(tool.name))
    return names


def digest_of(system: str, tools) -> str:
    """A stable key for this system prompt and this tool surface.

    Tools are SORTED BY NAME first. Two runs with the same surface in a
    different order must produce the same digest, or every run misses.
    """
    payload = json.dumps(
        {"system": system or "", "tools": sorted(tool_names(tools))},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _now() -> float:
    return time.time()


def lookup(model: str, key: str) -> dict | None:
    """A usable entry, or None. Expired and backed-off entries are None."""
    entry = _ENTRIES.get((model, key))
    if entry is None:
        return None
    if entry.get("status") == "failed":
        if _now() < entry.get("retry_after", 0):
            return entry            # still in backoff; caller must not retry
        _ENTRIES.pop((model, key), None)
        return None
    if _now() >= entry.get("expires_at", 0):
        _ENTRIES.pop((model, key), None)
        return None
    return entry


def remember(model: str, key: str, name: str, ttl_s: float,
             margin_s: float) -> dict:
    entry = {"status": "ready", "name": name,
             # Expire EARLY by the refresh margin, so a step never lands
             # in the gap between the real expiry and the next create.
             "expires_at": _now() + max(ttl_s - margin_s, 1)}
    _ENTRIES[(model, key)] = entry
    return entry


def remember_failure(model: str, key: str, reason: str,
                     backoff_s: float) -> dict:
    entry = {"status": "failed", "reason": reason,
             "retry_after": _now() + backoff_s}
    _ENTRIES[(model, key)] = entry
    logger.warning("prompt cache unavailable for %s (%s); not retrying "
                   "for %.0fs", model, reason, backoff_s)
    return entry


def forget_all() -> None:
    """For tests. A cache that survives between tests is a shared fixture."""
    _ENTRIES.clear()


async def ensure(client, model: str, system: str, tool_declarations,
                 policy) -> str | None:
    """The cache resource name for this (system, tools), or None.

    None means "send the prompt inline this time" and is never an error:
    caching is an optimisation, and a run that cannot cache must still
    run. Every failure path returns None.
    """
    # `stored_cache` is off by default: Gemini's implicit caching covers
    # this workload and creates nothing that outlives the request. See
    # CachePolicy for the measurement.
    if not policy.enabled or not getattr(policy, "stored_cache", False):
        return None
    if not system and not tool_declarations:
        return None

    key = digest_of(system, tool_declarations)
    entry = lookup(model, key)
    if entry is not None:
        return entry["name"] if entry.get("status") == "ready" else None

    from google.genai import types as gt

    try:
        cache = await client.aio.caches.create(
            model=model,
            config=gt.CreateCachedContentConfig(
                system_instruction=system or None,
                tools=tool_declarations or None,
                ttl=f"{policy.ttl_s}s",
            ),
        )
    except Exception as e:                                  # noqa: BLE001
        # Includes the ordinary case of a prompt below the provider's
        # minimum cacheable size, which is not a fault and must not be
        # retried on every step for the rest of the run.
        remember_failure(model, key, f"{type(e).__name__}: {e}",
                         policy.failure_backoff_s)
        return None

    name = getattr(cache, "name", None)
    if not name:
        remember_failure(model, key, "create returned no name",
                         policy.failure_backoff_s)
        return None
    remember(model, key, name, policy.ttl_s, policy.refresh_margin_s)
    logger.info("prompt cache %s created for %s (ttl %ss)",
                name, model, policy.ttl_s)
    return name
