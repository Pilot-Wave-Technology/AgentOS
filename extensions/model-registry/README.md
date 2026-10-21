# Model Registry Plugin

OpenClaw plugin that fetches provider + model definitions from the AgentOS
control panel and contributes them to `<agent-dir>/models.json` via the
`resolve_providers` hook — **without** requiring a gateway restart when the
admin adds, edits, or removes an entry in the catalog.

Sister endpoint on the control panel side: `/internal/model_catalog` (backed
by the seven registry tables in `agentos`).

## Files

| File | Purpose |
|---|---|
| [`index.ts`](index.ts) | Plugin entry. Registers `gateway_start` / `gateway_stop` / `resolve_providers` hooks and the `models.reload` RPC. Owns the 5-minute refresh timer. |
| [`src/cache.ts`](src/cache.ts) | Module-scope `Record<string, ProviderConfig>` cache. `refreshCache` fetches `/internal/model_catalog`, validates the shape, and swaps atomically. `getCachedProviders` returns a shallow clone. |
| [`src/types.ts`](src/types.ts) | `ModelCatalogResponse` shape + `assertCatalogShape` structural guard. |
| [`openclaw.plugin.json`](openclaw.plugin.json) | Plugin manifest (id, name, empty configSchema). |
| [`package.json`](package.json) | Plugin package metadata; no runtime deps. |

## Endpoint contract

```
GET ${CONTROL_PANEL_URL}/internal/model_catalog
Authorization: Bearer ${OPENCLAW_GATEWAY_TOKEN}
```

Response (JSON, HTTP 200):

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "https://ollama.corp.example",
      "apiKey": "OLLAMA_API_KEY",
      "models": [
        { "id": "llama3.3:8b" },
        { "id": "qwen2.5-coder:32b" }
      ]
    },
    "anthropic": {
      "apiKey": "ANTHROPIC_API_KEY",
      "models": [{ "id": "claude-opus-4-7" }]
    }
  }
}
```

`providers` values are `ProviderConfig` entries — the same shape openclaw
already writes to `<agent-dir>/models.json` under `providers[<id>]`. Extra
top-level metadata (e.g. `contractVersion`, `generatedAt`) is accepted and
ignored — only `providers` reaches the merge pipeline.

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `CONTROL_PANEL_URL` | yes (else plugin disables itself) | Base URL of the AgentOS control panel. Trailing slash is stripped. |
| `OPENCLAW_GATEWAY_TOKEN` | yes | Bearer token sent with every fetch. **Not** `OPENCLAW_GATEWAY_AUTH_TOKEN`. |

If `CONTROL_PANEL_URL` is unset, the plugin logs a `warn` on
`gateway_start` and returns `{ providers: {} }` from every hook invocation.
`models.json` collapses to today's `implicit + explicit` two-way merge —
byte-identical to a gateway that never loaded the plugin.

## Reload behaviour

- **On `gateway_start`**: single fetch of the catalog. Failures are logged
  at `warn` and the cache stays empty until the next refresh succeeds.
- **Every 5 minutes**: periodic refresh via `setInterval`. Failures logged
  at `debug` (self-heal noise); the previous cache is left untouched.
- **`models.reload` gateway RPC** (WebSocket, no payload):
  - success → `respond(true, { ok: true, count: <n> })`
  - failure → `respond(false, undefined, errorShape(UNAVAILABLE, msg))`
  - AgentOS should push this whenever the catalog changes so new
    definitions land before the next natural planning pass.

Newly-loaded providers appear on the **next** `planOpenClawModelsJson`
pass — the plugin does not force a re-plan on reload. That keeps the
plugin decoupled from the models-config write pipeline.

## Failure modes

| Situation | Behaviour |
|---|---|
| `CONTROL_PANEL_URL` unset | `warn` logged on start; hook returns `{}`; models.json unaffected. |
| Initial fetch fails (control panel offline, 5xx, network error) | `warn` logged; cache stays empty; periodic refresh retries. |
| Periodic refresh fails after a prior success | `debug` logged; last-known-good cache is preserved. |
| Response is not JSON or is missing `providers` object | `refreshCache` throws; cache unchanged. |
| Plugin handler throws during `resolve_providers` | Runner catches, logs via `plugins.diagnostics`, and moves on. |

No error path in this plugin fails a `models.json` write.

## Precedence

The `resolve_providers` runner enforces
**`explicit > plugin > implicit`** precedence in `models-config.plan.ts`:

- Any provider key the operator declared in
  `~/.openclaw/openclaw.json` under `config.models.providers.<id>` wins.
- Otherwise, this plugin's cached entry wins over an implicit
  (auto-discovered) entry with the same key.

If two plugins both contribute a `resolve_providers` handler and both
return the same provider key, the runner's tie-break is
**first-wins by priority DESC then registration order**; the loser's key
is logged as a `warn`-level diagnostic on `registry.diagnostics`.

## Gotchas for agentos authors

- `apiKey` values must be **env-var names**, not resolved secrets. For
  example, return `"ANTHROPIC_API_KEY"`, not `"sk-ant-..."`. openclaw's
  `normalizeProviders` will collapse `"${VAR}"`-style markers to the
  canonical env-var name, but a plaintext secret would land verbatim in
  `models.json` and leak.
- Do not include per-provider secrets in the response — the gateway VM
  resolves them at agent-invocation time from its own env / auth-profile
  storage.
- `providers` may include `baseUrl`, `apiKey`, `headers`, and `models[]`
  fields (openclaw's `ProviderConfig` shape). Additional per-provider
  fields are passed through unchanged; unknown fields at higher levels
  than `providers` are silently dropped.
