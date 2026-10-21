"""Build the wire contract emitted to the OpenClaw gateway.

Turns active `models` rows (family B) into the JSON dicts served by
`GET /internal/model_catalog`. Both a base model's own entry AND every
context-window variant it exposes are built here — there is no separate
`models`/`model_providers` row per variant to read (a variant is not a
separate registry entry, see `management.add_context_window_v2`'s
docstring), so `synthesizer.synthesize_all_variants` is the ONLY source
of per-variant wire data, computed fresh on every request.

Every wire dict is built from a HARDCODED key list — never `dict(row)`,
`**row`, or any dynamic key-splat. This prevents an unrelated column
added to `models` in some future migration from silently leaking to the
gateway.

The whitelist is:
    per model (flat `models` dict):
        id, name, contextWindow, reasoning, input
    per provider (`providers` dict) — a full `ProviderConfig`-shaped
    entry, NOT the earlier {upstream, upstreamId, contextWindow} shape:
        baseUrl, apiKey, api,
        models: [{id, name, upstreamId, contextWindow, reasoning, input}, ...]

`reasoning`/`input` close the capability-metadata gap found reconciling
this contract with the OpenClaw gateway's static
`infra/config/models-synthetic.json` — see
`documentation/MODEL_REGISTRY_CONTRACT.md`. They're derived from the
model's `tags` (`migrations/model_tags_table.sql`), not stored directly.

**The `providers` shape was corrected (§7a of the contract doc) after
tracing the actual gateway consumer**
(`agent_os_openclaw/extensions/model-registry`): its `resolve_providers`
hook merges this dict DIRECTLY as `Record<string, ProviderConfig>` — the
type openclaw's own `models.json` provider entries use, which requires
`baseUrl`/`apiKey`/`api`/`models[]`. The original {upstream, upstreamId,
contextWindow} shape structurally could never produce a usable model on
the gateway side (no `models` array for Pi to resolve a request against)
— a pre-existing gap in the Phase A1/A2 wire design, not something any
of the model-registry gap-closure work introduced.

**`apiKey` is a plain env-var-name string** (e.g. "ANTHROPIC_API_KEY"),
not a `{source, provider, id}` secret-ref object — see
`build_providers_payload`'s docstring for why the object form is
silently unusable on the gateway's live auth-resolution path even
though it round-trips fine through config storage.

Repointed at family B by the §7 item 1 gap-closure — see
`documentation/MODEL_REGISTRY_CONTRACT.md`.
"""
import logging
from typing import Any

from app.services.model_registry.synthesizer import synthesize_all_variants

_log = logging.getLogger(__name__)

# The only `api` values the gateway will accept on a provider entry — its
# `ModelApiSchema` is a closed `z.enum(MODEL_APIS)`
# (agent_os_openclaw/src/config/zod-schema.core.ts). Anything else is rejected.
#
# Why this list has to exist here at all, and why it is worth the drift risk:
# the sync writes the WHOLE config document in one `config.set` (F2), so the
# gateway validates all providers together. A single row with an unrecognised
# `api` fails validation for the entire document — every other provider's
# changes are rejected with it, and the soft-fail in
# `gateway_sync.sync_provider_auth_configs_to_gateway` swallows the error, so
# provider sync silently stops working platform-wide until someone reads the
# logs. That is exactly what happened on 2026-08-09 with a provider carrying
# `api_type='cli'`-style values the admin UI used to offer.
#
# NOTE: this is a hand-maintained copy of an upstream enum and can drift if a
# new api type is added there. That is a real weakness, accepted because the
# alternative (an RPC to ask the gateway) is disproportionate. `cli` is
# deliberately absent: AgentOS uses it to mark CLI-backed providers, which are
# never emitted (they have no `base_url`), and the gateway does not know it.
_GATEWAY_ACCEPTED_API_TYPES = frozenset({
    "openai-completions",
    "openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
    "google-generative-ai",
    "github-copilot",
    "bedrock-converse-stream",
    "ollama",
})


def _provider_entry(native: Any) -> dict:
    """One gateway `models.providers.<id>` entry, minus its model list.

    `apiKey` is OMITTED entirely when the provider has no `api_key_source`,
    never emitted as null. That distinction is load-bearing: the gateway
    types it as `SecretInputSchema.optional()`, and Zod's `.optional()`
    accepts an absent key but REJECTS an explicit null. Sending null would
    fail validation for the whole document — and since the sync now writes
    with `config.set` (one full-document write), a single such provider
    would take down the entire provider sync rather than just itself.
    """
    entry: dict = {
        "baseUrl": native["base_url"],
        "api": native["api_type"],
        "models": [],
    }
    if native["api_key_source"]:
        entry["apiKey"] = native["api_key_source"]
    return entry

# The gateway's own default when a model definition omits these fields
# entirely (`applyModelDefaults` in agent_os_openclaw's src/config/defaults.ts):
# reasoning=false, input=["text"] only. Emitting that same default when a
# model has neither the 'reasoning' nor 'vision' tag means an untagged
# model behaves identically whether or not this contract even carries
# these fields — no silent capability change for models nobody's tagged
# yet.
_DEFAULT_INPUT: tuple[str, ...] = ("text",)


def _capability_fields(tags: list[str]) -> dict[str, Any]:
    """Translate a model's tags into the wire contract's `reasoning`/
    `input` fields. Two tag names are wire-meaningful (see
    `migrations/model_tags_table.sql`):
        'reasoning' -> reasoning: true
        'vision'    -> "image" added to input (text always included)
    Any other tag is pure admin-side categorization, ignored here.
    """
    tags = tags or []
    input_modalities = list(_DEFAULT_INPUT)
    if "vision" in tags:
        input_modalities.append("image")
    return {
        "reasoning": "reasoning" in tags,
        "input": input_modalities,
    }


def build_models_payload(base_rows: list[Any]) -> dict[str, dict]:
    """Map active base rows onto the flat `models` payload — one entry
    for the base model itself, plus one per context-window variant
    `synthesize_all_variants` computes for it.

    Each entry has EXACTLY five fields: `id`, `name`, `contextWindow`,
    `reasoning`, `input`. Any other column present on a row is
    deliberately dropped. A variant's capability fields come from its own
    `tags` (propagated from the base by `synthesize_all_variants` — same
    capabilities, different context-window size).

    Note: as of the §7a gateway-consumer trace, the one real consumer
    (`agent_os_openclaw`'s `model-registry` plugin) reads only the
    `providers` section of the wire response — this flat `models` dict
    is currently unused by it. Kept as part of the documented contract
    shape (§1) for any other/future consumer; not removed speculatively.
    """
    out: dict[str, dict] = {}
    for row in base_rows:
        base_id = row["id"]
        out[base_id] = {
            "id": base_id,
            "name": row["label"],
            "contextWindow": row["context_window"],
            **_capability_fields(row.get("tags")),
        }
        for variant in synthesize_all_variants(row):
            out[variant["variant_id"]] = {
                "id": variant["variant_id"],
                "name": variant["label"],
                "contextWindow": variant["context_window"],
                **_capability_fields(variant.get("tags")),
            }
    return out


def build_providers_payload(
    base_rows: list[Any], native_providers: list[Any],
) -> dict[str, dict]:
    """Map active base rows' context-window variants onto the
    `providers` payload — one entry per synthesized SYNTHETIC provider
    (e.g. `google-flash-100k`), each a full `ProviderConfig`-shaped dict:

        {
          "baseUrl": "...", "apiKey": {"source": "env", ...}, "api": "...",
          "models": [{"id", "name", "upstreamId", "contextWindow",
                      "reasoning", "input"}, ...]
        }

    `baseUrl`/`apiKey`/`api` come from `native_providers` (rows from
    `queries.fetch_native_provider_configs`), looked up by the variant's
    upstream owner (e.g. 'google') — these three fields are constant per
    native upstream, so every synthetic provider sharing that upstream
    gets the identical triplet.

    `apiKey` is emitted as a PLAIN STRING equal to the env var name (e.g.
    "ANTHROPIC_API_KEY"), NOT the `{"source": "env", "provider": "default",
    "id": ...}` secret-ref object `models-synthetic.json` uses. That object
    shape looked equivalent but isn't: `model-auth.ts::getCustomProviderApiKey`
    (the function `resolveApiKeyForProvider` actually calls at auth time)
    only ever normalizes `apiKey` through `normalizeOptionalSecretInput`,
    which returns `undefined` for anything that isn't already a string —
    the ref-object form is silently treated as "no key" for EVERY provider,
    static or dynamic (confirmed live: a brand-new agent using a
    context-window variant failed with "No API key found for provider
    ...", even though `models.providers.<id>.apiKey` was populated). The
    object shape only ever resolves inside `normalizeProviders()`
    (`models-config.providers.ts`), which builds the per-agent `models.json`
    file for pi-ai — a completely different code path from the live
    `cfg.models.providers` reads `model-auth.ts` performs, and its output
    never feeds back into the config `model-auth.ts` sees.

    A plain string DOES work, though: `getCustomProviderApiKey` falls
    through to `resolveUsableCustomProviderApiKey`, which treats a string
    matching a known env-var name (`isKnownEnvApiKeyMarker`, e.g.
    "ANTHROPIC_API_KEY"/"GEMINI_API_KEY" — both listed in
    `model-auth-env-vars.ts::PROVIDER_ENV_API_KEY_CANDIDATES`) as a marker
    and resolves it via `process.env[marker]` directly. `api_key_source`
    is already exactly that env var name, so this needs no new data — just
    emitting it unwrapped.

    A variant whose upstream has no matching native provider row (e.g.
    the native row is disabled, or its `base_url`/`api_key_source` is
    NULL — true for CLI-kind natives, though those never have
    context-window variants in practice since they lack
    `provider_shorthand`) is skipped rather than emitted with missing
    connection details — a partial/broken provider entry would be worse
    than no entry.

    **Native providers ALSO get a top-level entry now** (e.g. `anthropic`),
    listing every active base row under that upstream at its own native
    `context_window`, bare id (e.g. `claude-opus-5`, matching what's
    requested when a bare-base-id — no context-window suffix — model is
    selected; see `dropdowns.build_go_model_tree_v2`'s "native entry").
    This used to be considered unnecessary ("the gateway resolves those
    itself with its own env-configured key") — true for models the
    gateway ships with built-in knowledge of, but NOT for a model
    invented later purely through this Model Registry (confirmed live:
    selecting the bare-id "native" option for a custom-added base model
    like `anthropic/claude-opus-5` silently fell back to the default
    model — there was no gateway-side provider entry for the plain
    `anthropic` key at all, only for its synthesized variants). Additive
    only: if the gateway already has real built-in knowledge of a given
    native provider/model, `config.patch`'s merge-by-id semantics
    (`gateway_sync.sync_provider_auth_configs_to_gateway`) layer on top
    rather than replacing it.
    """
    # `base_url` is genuinely required — it's `z.string().min(1)` in the
    # gateway's provider schema, so a row without one cannot produce a valid
    # entry at all. CLI providers (claude-cli) have none, which is correct:
    # they shell out to a local binary and have no HTTP endpoint to describe.
    #
    # `api_key_source` is deliberately NOT part of this filter (changed
    # 2026-08-09). It used to be, and that was the cause of a silent failure:
    # the admin UI has no input for `api_key_source` — only the seed
    # migration ever sets it — so ANY provider added through the UI had it
    # NULL and was dropped here entirely. It never reached the gateway, while
    # its credential badge still read healthy, because the credential payload
    # (`build_provider_credentials_payload`) filters on `secret_id` instead
    # and had no idea this one had been discarded.
    #
    # Emitting it without an `apiKey` is strictly better than dropping it:
    # a gateway carrying the Model Registry's variant-auth resolution
    # (`agent_os_openclaw/src/agents/model-registry-variant-auth.ts`) resolves
    # the credential from the upstream provider id anyway and needs no label;
    # an older gateway fails the call with an explicit "no API key found for
    # provider" instead of silently falling back to its default model, which
    # is the far worse outcome and the one this project has been bitten by
    # before.
    # `api_type` must be one the gateway's schema actually accepts, or the
    # whole write fails — see _GATEWAY_ACCEPTED_API_TYPES. Skipping the row
    # costs one provider; sending it costs every provider.
    native_by_id = {
        row["id"]: row for row in native_providers
        if row["base_url"] and row["api_type"] in _GATEWAY_ACCEPTED_API_TYPES
    }
    for row in native_providers:
        if row["base_url"] and row["api_type"] not in _GATEWAY_ACCEPTED_API_TYPES:
            _log.warning(
                "Provider %r has api_type=%r, which the gateway's schema does not "
                "accept (valid: %s) — skipping it. Every model under this provider "
                "is unavailable on the gateway until its api_type is corrected.",
                row["id"], row["api_type"], ", ".join(sorted(_GATEWAY_ACCEPTED_API_TYPES)),
            )
            continue
        if row["base_url"] and not row["api_key_source"]:
            _log.warning(
                "Provider %r has no api_key_source — emitting it to the gateway "
                "without an apiKey label. It will authenticate only on a gateway "
                "that resolves variant/provider auth from the upstream id; "
                "otherwise calls to it fail with 'no API key found'.",
                row["id"],
            )

    out: dict[str, dict] = {}
    for row in base_rows:
        for variant in synthesize_all_variants(row):
            provider_key = variant["provider_key"]
            upstream, upstream_id = variant["upstream_id"].split("/", 1)
            native = native_by_id.get(upstream)
            if native is None:
                continue

            model_id = variant["variant_id"].split("/", 1)[1]
            model_entry = {
                "id": model_id,
                "name": variant["label"],
                "upstreamId": upstream_id,
                "contextWindow": variant["context_window"],
                **_capability_fields(variant.get("tags")),
            }

            if provider_key not in out:
                out[provider_key] = _provider_entry(native)
            out[provider_key]["models"].append(model_entry)

    for row in base_rows:
        owner, native_name = row["id"].split("/", 1)
        native = native_by_id.get(owner)
        if native is None:
            continue

        model_entry = {
            "id": native_name,
            "name": row["label"],
            "upstreamId": native_name,
            "contextWindow": row["context_window"],
            **_capability_fields(row.get("tags")),
        }
        if owner not in out:
            out[owner] = _provider_entry(native)
        out[owner]["models"].append(model_entry)

    return out


def build_provider_credentials_payload(native_providers: list[Any]) -> dict[str, dict]:
    """Build the `providerCredentials` section of `/internal/model_catalog` —
    Secret Manager REFERENCES only, never a resolved value.

    Keyed by real upstream provider id (`anthropic`, `google`), NOT by the
    synthesized variant/provider-alias ids `build_providers_payload` uses —
    credential resolution is a per-upstream concept (one secret per real
    provider), so this deliberately has a different shape/granularity from
    that function's output.

    The gateway's `model-registry` plugin (`agent_os_openclaw/extensions/
    model-registry`) reads this section, resolves each reference directly
    against Secret Manager using its own GCP credentials, and reports
    credential/model health back via the `models.reload` RPC response —
    AgentOS never sees the resolved value at any point. See
    research/model_registry_secret_management_design.md.

    Providers with no `secret_id` (e.g. `claude-cli`, which authenticates
    by shelling out to a local binary, not an API key) are omitted
    entirely rather than emitted with null fields — nothing for the
    gateway to resolve for those.

    A provider with `credential_status == 'revoked'` is likewise
    omitted — this is what actually makes the admin "Revoke" action
    stop the credential from being usable: the next `models.reload`
    (triggered by the same route that set 'revoked') no longer hands
    the gateway a reference to resolve, so its plugin stops re-injecting
    the value into its own auth env on subsequent refresh ticks. Secret
    Manager itself is untouched — the secret still resolves fine if
    queried directly — this only stops OUR gateway from doing so. See
    `revoke_provider_credential_v2`'s docstring in management.py.
    """
    out: dict[str, dict] = {}
    for row in native_providers:
        if not row["secret_id"]:
            continue
        if row.get("credential_status") == "revoked":
            continue
        out[row["id"]] = {
            "secretProject": row["secret_project"],
            "secretId": row["secret_id"],
            "secretVersion": row["secret_version"],
        }
    return out
