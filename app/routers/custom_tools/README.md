# Custom Tools — Routers

Three route surfaces, all under `app/routers/custom_tools/`:

| Surface | Prefix | Auth | Consumer |
|---|---|---|---|
| Admin UI | `/custom-tools/*` | `require_admin` (Firebase JWT + admin role) | Browser (Jinja2 + HTMX) |
| JSON API | `/api/custom-tools/*` | `require_admin` | Admin UI, external scripts |
| Internal | `/internal/custom-tools/*` | Bearer (`OPENCLAW_AUTH_TOKEN`) | OpenClaw gateway plugin |

## Files

| File | Purpose |
|---|---|
| [`admin_routes.py`](admin_routes.py) | Server-rendered pages: tool list, new-tool forms, tool detail, credential list, OpenAPI import wizard, MCP import wizard. Detail pages include the `_manifest_review.html` partial (declared capabilities panel) and a Regenerate button on aggregated tools (`mcp_server`, `openapi`). The typescript wizard form has a structured capability builder (toggle per cap, list inputs for net hosts / mcp servers, credential dropdown for secrets, radio for storage mode) that serialises to a runtime manifest on save. Builds a `credentials_for_js` context var alongside the full `credentials` list so the typescript form's JS injection is JSON-safe. |
| [`api_routes.py`](api_routes.py) | Mounts the JSON sub-routers at `/api/custom-tools` — credentials, openapi import, mcp import — and registers the tool CRUD handlers directly. |
| [`api_tools.py`](api_tools.py) | Tool CRUD: list / get / create / update / disable / enable / test / regenerate / bulk-credential. Every mutating call triggers `tools.reload` on the gateway. `api_test_tool` delegates to the gateway's `tools.test_execute` RPC. `api_regenerate_tool` re-runs the tier's generator against current (or partially-updated) config + credential and persists a fresh `source_code` + `manifest`. |
| [`api_openapi.py`](api_openapi.py) | OpenAPI import flow: `POST /openapi/parse` (preview), `POST /openapi/specs` (store), `POST /openapi/generate` (create one aggregated tool from selected operations). |
| [`api_mcp.py`](api_mcp.py) | MCP import flow: `POST /mcp/discover` (preview via SSE or stdio, with optional stored credential), `POST /mcp/generate` (create one aggregated mcp_server tool). |
| [`api_credentials.py`](api_credentials.py) | Credential CRUD: list / create / rotate / delete / usage-count. Plaintext never leaves the request body on create / rotate. |
| [`internal_routes.py`](internal_routes.py) | `GET /internal/custom-tools` (list enabled tools for the plugin), `GET /internal/custom-tools/credentials/by-name/{n}` (plaintext credential for the runtime's `secrets.get`), and `POST /internal/custom-tools/audit` (batched capability events from the plugin — forwarded into the existing ledger as `tool.capability_called` events). All Bearer-token auth. The execute endpoint was removed in phase 4 — execution lives in the OpenClaw plugin's capability runtime. |

## Route → handler map

```
GET    /custom-tools                                  → admin_routes.custom_tools_list
GET    /custom-tools/new/{tier}                       → admin_routes.custom_tool_new   (http_action | typescript | openapi)
GET    /custom-tools/{tool_name}                      → admin_routes.custom_tool_detail
GET    /custom-tools/credentials                      → admin_routes.credentials_list
GET    /custom-tools/credentials/new                  → admin_routes.credentials_new
GET    /custom-tools/openapi/import                   → admin_routes.openapi_import
GET    /custom-tools/mcp/import                       → admin_routes.mcp_import

GET    /api/custom-tools                              → api_tools.api_list_tools
POST   /api/custom-tools                              → api_tools.api_create_tool
GET    /api/custom-tools/{tool_name}                  → api_tools.api_get_tool
PATCH  /api/custom-tools/{tool_name}                  → api_tools.api_update_tool
POST   /api/custom-tools/{tool_name}/test             → api_tools.api_test_tool  (delegates to gateway tools.test_execute RPC)
POST   /api/custom-tools/{tool_name}/regenerate       → api_tools.api_regenerate_tool  (re-runs the tier generator against current config, refreshes source_code + manifest)
POST   /api/custom-tools/{tool_name}/disable          → api_tools.api_disable_tool
POST   /api/custom-tools/{tool_name}/enable           → api_tools.api_enable_tool
POST   /api/custom-tools/bulk/credential              → api_tools.api_bulk_set_credential

GET    /api/custom-tools/credentials                  → api_credentials.api_list_credentials
POST   /api/custom-tools/credentials                  → api_credentials.api_create_credential
POST   /api/custom-tools/credentials/{id}/rotate      → api_credentials.api_rotate_credential
DELETE /api/custom-tools/credentials/{id}             → api_credentials.api_delete_credential
GET    /api/custom-tools/credentials/{id}/usage       → api_credentials.api_credential_usage

POST   /api/custom-tools/openapi/parse                → api_openapi.api_parse_openapi
POST   /api/custom-tools/openapi/specs                → api_openapi.api_create_openapi_spec
POST   /api/custom-tools/openapi/generate             → api_openapi.api_generate_openapi_tools

POST   /api/custom-tools/mcp/discover                 → api_mcp.api_mcp_discover
POST   /api/custom-tools/mcp/generate                 → api_mcp.api_mcp_generate

GET    /internal/custom-tools                         → internal_routes.list_tools                (Bearer)
GET    /internal/custom-tools/credentials/by-name/{n} → internal_routes.get_credential_by_name    (Bearer)
POST   /internal/custom-tools/audit                   → internal_routes.ingest_audit              (Bearer)
```

After phase 4 the plugin no longer POSTs back to AgentOS to execute a tool —
that endpoint is gone. All execution happens locally in the plugin's
capability runtime ([`agent_os_openclaw/extensions/custom-tools/src/runtime/`](../../../../agent_os_openclaw/extensions/custom-tools/src/runtime/)).
AgentOS only serves the tool list and per-call credential plaintext.

## Reload signaling

Every create / update / disable / enable / bulk-credential call ends with
`await gateway_admin.tools_reload()`. That triggers the `tools.reload` RPC on the
OpenClaw gateway, which calls back to `GET /internal/custom-tools` to refresh the
in-memory cache. If the gateway is briefly unreachable the plugin's 5-minute
periodic refresh picks up the change instead — see
[`agent_os_openclaw/extensions/custom-tools/README.md`](../../../../agent_os_openclaw/extensions/custom-tools/README.md).

## Path ordering

`admin_routes.py` carefully orders routes so that the parameterised
`/custom-tools/{tool_name}` doesn't shadow the fixed prefixes (`/credentials`,
`/openapi/import`, `/mcp/import`, `/new/{tier}`). Keep the parameterised route
**last** when adding new fixed paths.

## Related

- Service layer: [`app/services/custom_tools/README.md`](../../services/custom_tools/README.md)
- OpenClaw-side plugin: [`agent_os_openclaw/extensions/custom-tools/README.md`](../../../../agent_os_openclaw/extensions/custom-tools/README.md)
- Overview + sequence diagrams: [`documentation/CUSTOM_TOOLS.md`](../../../documentation/CUSTOM_TOOLS.md)
