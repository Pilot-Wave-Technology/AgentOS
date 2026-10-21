"""asyncpg queries for the model registry tables.

Repointed (§7 item 1 of the gap review,
`documentation/MODEL_REGISTRY_CONTRACT.md`) from the frozen Phase-A1
`model_registry` / `model_provider_registry` tables ("family A") to the
actively-maintained `models` table ("family B") that backs the admin UI
and the §5a live dropdowns. Family A stays in the DB (additive-only
migration rules) but no longer feeds the gateway pull endpoint — editing
a model through the admin UI now actually changes what
`GET /internal/model_catalog` returns.

A single query (`fetch_active_base_models`) is the only DB read the
emitter needs: base rows carry everything required to synthesize every
context-window variant's wire entries on the fly
(`synthesizer.synthesize_all_variants`) — there is no separate
`model_providers` read, because a context-window variant has no row of
its own to read (see `management.add_context_window_v2`'s docstring).

Every query selects ONLY the columns the wire contract consumes — no
`SELECT *`, no room for column drift to leak downstream.
"""
from typing import Any


async def fetch_active_base_models(pool) -> list[Any]:
    """Return every active `models` row with the columns needed to build
    both wire-payload sections: `id`, `label`, `context_window` (feeds
    `emitter.build_models_payload` directly) plus `provider_shorthand` +
    `available_context_windows` (feeds `synthesizer.synthesize_all_variants`,
    which both payload builders call to enumerate context-window
    variants) and `tags` (feeds the wire contract's `reasoning`/`input`
    fields — see `emitter._capability_fields`).

    `available_context_windows` and `tags` are each read via the same
    junction-table subquery pattern `management._BASE_MODEL_V2_COLS`
    uses — the frozen `models.available_context_windows` array column is
    never read.
    """
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, label, context_window, provider_shorthand, "
            "  (SELECT COALESCE(array_agg(cw.tokens ORDER BY cw.tokens), "
            "                   '{}'::integer[]) "
            "     FROM model_context_windows cw "
            "    WHERE cw.model_id = models.id) AS available_context_windows, "
            "  (SELECT COALESCE(array_agg(mt.tag_name ORDER BY mt.tag_name), "
            "                   '{}'::text[]) "
            "     FROM model_tags mt "
            "    WHERE mt.model_id = models.id) AS tags "
            "  FROM models "
            " WHERE status = 'active' "
            " ORDER BY id"
        )


async def fetch_native_provider_configs(pool) -> list[Any]:
    """Return the connection details (`base_url`, `api_key_source`,
    `api_type`) for every active provider — every `model_providers` row
    IS a native upstream now (the `kind` column, which used to
    distinguish 'native' from a hypothetical 'synthetic_origin' that
    nothing ever used, was dropped entirely). The gateway-facing
    `providers` payload nests a full model list under each synthetic
    provider (`emitter.build_providers_payload`), and those connection
    fields are constant per upstream (e.g. every `google-flash-*`
    synthetic provider routes through the same real Google endpoint/key).

    CLI providers (`claude-cli`, `codex-cli`) have NULL `base_url`/
    `api_key_source` — they never have context-window variants (no
    provider_shorthand set), so they're never looked up by
    `build_providers_payload` in practice, but the caller should not
    assume non-null here.
    """
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, base_url, api_key_source, api_type, "
            "       secret_project, secret_id, secret_version, "
            "       credential_status "
            "  FROM model_providers "
            " WHERE status = 'active' "
            " ORDER BY id"
        )


async def fetch_all_provider_owner_ids(pool) -> list[Any]:
    """Return every `model_providers.id`, REGARDLESS of `status`.

    Used only to recognize AgentOS's own synthetic-provider naming convention
    (`<owner>-<shorthand>-<size>`, e.g. `anthropic-sonnet-1m`) when pruning stale
    entries from the gateway's `models.providers` (`gateway_sync.py`). A disabled
    provider's leftover synthetic keys must still be recognized as ours so they
    can be pruned — `fetch_native_provider_configs`'s `status = 'active'` filter
    would hide exactly the owners whose stale variants most need cleaning up.
    """
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT id FROM model_providers ORDER BY id")
