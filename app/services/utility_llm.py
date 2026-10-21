"""Generic provider dispatch for utility-purpose LLM calls.

`app/services/misc_models.py` resolves WHICH model a purpose should use
(a wire id like `google/gemini-2.5-flash` or `anthropic/claude-sonnet-4-5`,
picked from the same `models` registry table the Registry tab manages).
This module is the other half: given that wire id, resolve its
credential (via the registry's `model_providers` Secret Manager
reference — `app/services/model_registry/secret_resolver.py`) and call
the right provider SDK.

Only providers with real API-call support are implemented below (Google
Gemini, Anthropic). A wire id for any other registry provider (e.g. a
CLI-only provider, or one with no direct-SDK integration here) raises
`UtilityLLMError` with a clear message — callers already wrap these
calls in try/except and fall back to their own default behavior (see
each call site's docstring), so this never needs a silent guess.
"""
import logging
import os

from app.database import get_pool
from app.services.misc_models import SUPPORTED_UTILITY_PROVIDERS, get_misc_model
from app.services.model_registry.queries import fetch_native_provider_configs
from app.services.model_registry.secret_resolver import resolve_provider_secret

logger = logging.getLogger(__name__)

_ANTHROPIC_MAX_TOKENS = 4096


class UtilityLLMError(Exception):
    pass


async def _resolve_api_key(pool, provider_id: str) -> str:
    """Registry Secret Manager reference first; falls back to the
    provider's legacy env var if the registry has no reference configured
    for it yet (keeps Gemini working exactly as before this module
    existed, for environments that never set up a `model_providers`
    secret ref for `google`)."""
    providers = await fetch_native_provider_configs(pool)
    row = next((p for p in providers if p["id"] == provider_id), None)

    if row is not None:
        secret_value = await resolve_provider_secret(
            row["secret_project"], row["secret_id"], row["secret_version"]
        )
        if secret_value:
            return secret_value

    if provider_id == "google":
        return os.environ.get("GEMINI_API_KEY", "")
    if provider_id == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return ""


async def _call_google(model_name: str, api_key: str, prompt: str, system_instruction: str | None, json_mode: bool) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    config = None
    if system_instruction or json_mode:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json" if json_mode else None,
        )
    response = await client.aio.models.generate_content(
        model=model_name, contents=prompt, config=config,
    )
    return (response.text or "").strip()


async def _call_anthropic(model_name: str, api_key: str, prompt: str, system_instruction: str | None, json_mode: bool) -> str:
    import anthropic

    system = system_instruction or ""
    if json_mode:
        system = (system + "\n\nRespond with ONLY valid JSON — no markdown fences, no commentary.").strip()

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model_name,
        max_tokens=_ANTHROPIC_MAX_TOKENS,
        system=system or anthropic.NOT_GIVEN,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text.strip()


async def generate_utility_text(
    purpose_key: str,
    prompt: str,
    *,
    system_instruction: str | None = None,
    json_mode: bool = False,
) -> str:
    """Resolve `purpose_key`'s configured model and call it. Raises
    `UtilityLLMError` on any failure (unresolvable credential, unsupported
    provider, or the provider SDK call itself failing) — every current
    call site already wraps its own call in try/except and degrades to a
    fallback, so this never needs to degrade internally."""
    pool = get_pool()
    wire_id = await get_misc_model(pool, purpose_key)
    if "/" not in wire_id:
        raise UtilityLLMError(f"malformed wire id for purpose={purpose_key!r}: {wire_id!r}")
    provider_id, model_name = wire_id.split("/", 1)

    if provider_id not in SUPPORTED_UTILITY_PROVIDERS:
        raise UtilityLLMError(
            f"unsupported provider for utility model calls: {provider_id!r} "
            f"(purpose={purpose_key!r}, model={wire_id!r}) — only "
            f"{sorted(SUPPORTED_UTILITY_PROVIDERS)} have a direct-SDK integration "
            f"in this module"
        )

    # Credential resolution reaches Secret Manager over the network, so it
    # fails the same transient ways an SDK call does — wrapped for the same
    # reason as the dispatch below.
    try:
        api_key = await _resolve_api_key(pool, provider_id)
    except Exception as exc:
        raise UtilityLLMError(
            f"credential lookup failed for provider={provider_id!r} "
            f"(purpose={purpose_key!r}): {exc}"
        ) from exc
    if not api_key:
        raise UtilityLLMError(
            f"no credential available for provider={provider_id!r} "
            f"(purpose={purpose_key!r}, model={wire_id!r})"
        )

    logger.info("utility_llm: purpose=%s provider=%s model=%s", purpose_key, provider_id, model_name)

    # Translate every provider-SDK failure into UtilityLLMError so this
    # function actually honours the single-exception contract its docstring
    # promises. Without this the raw SDK exception (google.genai /
    # anthropic transport, rate-limit and API errors) escaped untouched,
    # and the five call sites that narrowed their handler from
    # `except Exception` to `except UtilityLLMError` during the cutover —
    # gemini_client.py's three entry points, memory.py's compaction, and
    # resource_scanner.py's summarizer — silently lost their graceful
    # degradation: a transient provider blip stopped returning "" and
    # started propagating up into the caller's request instead.
    try:
        if provider_id == "google":
            return await _call_google(model_name, api_key, prompt, system_instruction, json_mode)
        return await _call_anthropic(model_name, api_key, prompt, system_instruction, json_mode)
    except Exception as exc:
        raise UtilityLLMError(
            f"{provider_id} call failed (purpose={purpose_key!r}, model={wire_id!r}): {exc}"
        ) from exc
