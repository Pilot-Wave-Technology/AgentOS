"""Utility-model settings — config for internal-purpose provider calls.

Separate from `app/services/model_registry/` ("models"/`model_providers`,
family B), which governs models an operator can assign to an *agent*
deployed via the AgentOS UI. This module governs the other category found
in the model-reference audit: AgentOS's own code calling a provider SDK
directly for a fixed internal purpose (conversation summarization,
template generation, memory compaction, Taximeter weightage recompute,
goal-summary reconciliation).

Each purpose keeps its own hardcoded default (the literal the call site
used before this module existed) as a fallback — this table only
overrides when an admin has explicitly set one, so an unreachable DB or a
never-configured purpose never breaks the call site.

Model ids stored here are the registry's WIRE form (`provider/model`, e.g.
`google/gemini-2.5-flash` or `anthropic/claude-sonnet-4-5`) — the same
`models.id` primary key the Registry tab already maintains.
`list_utility_model_options` reads that table directly (every active row
whose provider `app/services/utility_llm.py` can actually call — see
`SUPPORTED_UTILITY_PROVIDERS` below) rather than duplicating a second
list; adding or retiring a model is a normal Registry edit and just
appears/disappears here. Actually calling the resolved model (credential
lookup + provider SDK dispatch) is `utility_llm.py`'s job, not this
module's — this module only resolves "which wire id is configured for
this purpose."
"""
import logging
import time
from typing import Any

from app.services.model_registry.queries import fetch_active_base_models

logger = logging.getLogger(__name__)

# Providers this dropdown offers. utility_llm.py has a working SDK
# integration for both "google" and "anthropic", but only "google" is
# enabled here for now — it's the only one actually verified end-to-end
# (credential resolution + live calls) for these utility purposes today.
# The registry also carries `claude-cli` (not a direct-API provider at
# all — it shells out to the CLI binary) and may carry others with no SDK
# wired up here. Filtering the dropdown to this set means every option a
# purpose can be set to is guaranteed to actually work — no "looks
# selectable, silently falls back" gap. Add "anthropic" back once it's
# been verified — no other code change needed, utility_llm.py already
# supports it.
SUPPORTED_UTILITY_PROVIDERS: frozenset[str] = frozenset({"google"})

# Only used if the `models` table has no active rows at all (fresh/empty
# environment) — keeps the dropdown non-empty rather than broken. Not a
# normal-path allowlist.
DEFAULT_MODEL_ID_PLACEHOLDER = "google/gemini-3.8-flash"

# purpose_key -> label + default wire id. `default_model_id` is the
# literal each call site carried before adopting this module — kept in
# sync there as a fallback, not removed, so a DB outage degrades to
# today's behavior rather than failing the call.
MISC_MODEL_PURPOSES: dict[str, dict[str, Any]] = {
    "conversation_summary": {
        "label": "Conversation summarization (Assistant resume)",
        "default_model_id": "google/gemini-3.8-flash",
    },
    "thread_title": {
        "label": "Thread title generation",
        "default_model_id": "google/gemini-3.8-flash",
    },
    "goal_summary_reconcile": {
        "label": "Goal progress-summary reconciliation",
        "default_model_id": "google/gemini-3.8-flash",
    },
    "resource_scan_summary": {
        "label": "Resource-scan summarization",
        "default_model_id": "google/gemini-3.8-flash",
    },
    "template_authoring": {
        "label": "Template wizard generation",
        "default_model_id": "google/gemini-3.8-flash",
    },
    "memory_compaction": {
        "label": "Assistant memory compaction",
        "default_model_id": "google/gemini-3.8-flash",
    },
    "goal_summary_doc": {
        "label": "Goal document generation",
        "default_model_id": "google/gemini-3.8-flash",
    },
    # Consumed by agent_os_openclaw's extensions/misc-models/ plugin
    # (GET /internal/misc_model_settings), not by anything in this repo —
    # see agent_os_openclaw/src/agents/tools/web-search.ts::resolveGeminiModel.
    # The OpenClaw gateway only ever calls Gemini directly for grounded
    # search regardless of what's picked here — if this purpose is ever
    # set to a non-Google wire id, the gateway-side consumer ignores it
    # and falls back to its own hardcoded default (see that file).
    "web_search_grounding": {
        "label": "Web-search grounding (OpenClaw gateway, Gemini only)",
        "default_model_id": "google/gemini-3.8-flash",
    },
    # THE MODEL THE GATEWAY SUMMARISES WITH when it compacts a session
    # (`agents.defaults.compaction.model`, read through `extensions/
    # misc-models/` in agent_os_openclaw). The gateway calls it itself, so it
    # may be any provider the gateway runs -- not only the ones this
    # process has an SDK for -- and its effective value is ALWAYS served,
    # default included, so this page is the source of truth and the
    # provisioned config value only covers a gateway's first boot.
    "gateway_compaction": {
        "label": "Gateway compaction summarizer (OpenClaw gateway)",
        "default_model_id": "google/gemini-3.8-flash",
        "providers": frozenset({"google", "anthropic"}),
        "serve_default": True,
    },
}


def providers_for(purpose_key: str) -> frozenset[str]:
    """Which providers a purpose may pick from."""
    meta = MISC_MODEL_PURPOSES.get(purpose_key) or {}
    return meta.get("providers") or SUPPORTED_UTILITY_PROVIDERS

# In-process cache of purpose_key -> configured wire id. A DB read per
# call site invocation would add a query to every summarization/title/
# compaction call. A short TTL is enough to make an admin's edit take
# effect within a minute without a pub/sub invalidation path for a table
# this small and low-traffic.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, str] = {}
_cache_loaded_at: float = 0.0


async def _load_cache(pool) -> None:
    global _cache, _cache_loaded_at
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT purpose_key, model_id FROM misc_model_settings"
        )
    _cache = {row["purpose_key"]: row["model_id"] for row in rows}
    _cache_loaded_at = time.monotonic()
    logger.info("misc_models: cache refreshed, %d override(s)", len(_cache))


async def get_misc_model(pool, purpose_key: str) -> str:
    """Resolve the wire id (`provider/model`) configured for `purpose_key`.

    Falls back to `MISC_MODEL_PURPOSES[purpose_key]["default_model_id"]`
    if no override row exists, the DB is unreachable, or `purpose_key`
    is not registered (logged as a warning — a typo'd key should never
    fail the caller's actual work).
    """
    default = MISC_MODEL_PURPOSES.get(purpose_key, {}).get("default_model_id")
    if default is None:
        logger.warning("misc_models: unknown purpose_key=%s, no default registered", purpose_key)
        return ""

    global _cache_loaded_at
    if time.monotonic() - _cache_loaded_at > _CACHE_TTL_SECONDS:
        try:
            await _load_cache(pool)
        except Exception as exc:
            logger.warning("misc_models: cache refresh failed, using last-known/default — %s", exc)

    model_id = _cache.get(purpose_key, default)
    logger.debug("misc_models: purpose=%s resolved model=%s", purpose_key, model_id)
    return model_id


async def list_misc_models(pool) -> list[dict[str, Any]]:
    """Return every registered purpose with its currently effective wire id."""
    await _load_cache(pool)
    return [
        {
            "purpose_key": key,
            "label": meta["label"],
            "default_model_id": meta["default_model_id"],
            "model_id": _cache.get(key, meta["default_model_id"]),
            "is_override": key in _cache,
            "serve_default": bool(meta.get("serve_default")),
            "providers": sorted(providers_for(key)),
        }
        for key, meta in MISC_MODEL_PURPOSES.items()
    ]


async def list_utility_model_options(
    pool, providers: frozenset[str] = SUPPORTED_UTILITY_PROVIDERS,
) -> list[dict[str, Any]]:
    """Every active model in the `models` registry table whose provider is
    in `SUPPORTED_UTILITY_PROVIDERS` — the same "all the models we
    support" source of truth the Registry tab maintains, filtered down to
    what `app/services/utility_llm.py` can actually call. A provider like
    `claude-cli` (CLI-only, no direct API) or any future registry provider
    without an SDK integration here never appears as a selectable option,
    so nothing in this dropdown can silently fail at call time.

    Each entry: `{id (wire id "provider/model"), label, provider_id,
    context_window}`. `provider_id` is the `id` prefix before the first
    "/" — the registry's own `_MODEL_ID_RE` convention (see
    `management.py`), not re-derived from a separate column.

    Falls back to a single placeholder entry only if no active,
    supported-provider rows exist at all (fresh/empty environment) — the
    dropdown should never render with zero options.
    """
    rows = await fetch_active_base_models(pool)
    options = [
        {
            "id": row["id"],
            "label": row["label"],
            "provider_id": row["id"].split("/", 1)[0],
            "context_window": row["context_window"],
        }
        for row in rows
        if row["id"].split("/", 1)[0] in providers
    ]
    if options:
        return options
    return [
        {
            "id": DEFAULT_MODEL_ID_PLACEHOLDER,
            "label": DEFAULT_MODEL_ID_PLACEHOLDER,
            "provider_id": DEFAULT_MODEL_ID_PLACEHOLDER.split("/", 1)[0],
            "context_window": None,
        }
    ]


class MiscModelValidationError(Exception):
    pass


async def set_misc_model(pool, purpose_key: str, model_id: str) -> None:
    """Set (or clear, if `model_id` is empty) the override for `purpose_key`."""
    if purpose_key not in MISC_MODEL_PURPOSES:
        raise MiscModelValidationError(f"unknown purpose_key: {purpose_key!r}")

    if not model_id:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM misc_model_settings WHERE purpose_key = $1", purpose_key
            )
        logger.info("misc_models: cleared override for purpose=%s (reverts to default)", purpose_key)
    else:
        options = await list_utility_model_options(
            pool, providers_for(purpose_key))
        if model_id not in {o["id"] for o in options}:
            raise MiscModelValidationError(
                f"model_id {model_id!r} is not an active model in the "
                f"registry — add it on the Registry tab first"
            )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO misc_model_settings (purpose_key, model_id, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (purpose_key)
                DO UPDATE SET model_id = EXCLUDED.model_id, updated_at = now()
                """,
                purpose_key,
                model_id,
            )
        logger.info("misc_models: purpose=%s set to model=%s", purpose_key, model_id)

    global _cache_loaded_at
    _cache_loaded_at = 0.0
