# Custom Tools — Services

Backend logic for admin-defined tools. Custom tools live in the unified `tools`
table where `tier IS NOT NULL`. Catalog tools (synced from OpenClaw plugins) have
`tier IS NULL` and are managed separately by `app/services/tool_registry/`.

## Files

## Where execution happens (post-phase-4)

Execution moved **off** AgentOS Cloud Run and **into** the OpenClaw plugin's
capability runtime on the gateway VM. The files in this directory now handle:

- Persistence: `crud.py` (DB I/O for tools, credentials, OpenAPI specs)
- Credential encryption + decryption: `credential.py`
- SSRF validation for AgentOS-side egress (spec fetch only): `ssrf_guard.py`
- Spec parsing: `openapi_parser.py`
- MCP server probing for the import wizard: `mcp_discovery.py`
- Source/manifest generation per wizard tier: `generators/`

The previous `executor.py` (which routed execution per tier) is **deleted** —
the capability runtime in `agent_os_openclaw/extensions/custom-tools/src/runtime/`
owns all execution. The admin "Test Tool" button now invokes the
`tools.test_execute` RPC on the gateway instead of a local executor.

## Files

| File | Purpose |
|---|---|
| [`crud.py`](crud.py) | DB operations: create / read / update / status / bulk credential for custom tools; credential CRUD; OpenAPI spec storage. JSONB columns (`config`, `parameters_schema`, `manifest`) are JSON-encoded on write and decoded on read. `list_custom_tools()` returns the full unpaginated set regardless of `is_core` (used by the OpenClaw plugin sync and internal/admin APIs — must never be filtered); `count_custom_tools()` / `list_custom_tools_page()` are the paginated/searchable, `is_core = false`-filtered variants used only by the `/settings/tools` Custom Tools tab. |
| [`credential.py`](credential.py) | Fernet encrypt / decrypt for `tool_credentials.encrypted_value`. In-memory cache (5-min TTL) per `credential_id`. `inject_auth_header` helper retained for spec-fetch auth. |
| [`ssrf_guard.py`](ssrf_guard.py) | `assert_safe_url(url)` — used now only by the OpenAPI spec fetch path. Tool-call egress is enforced by the runtime's `net` capability host allowlist on the VM. |
| [`openapi_parser.py`](openapi_parser.py) | Fetch + parse a spec (URL or pasted JSON/YAML), extract operations, build per-operation JSON Schemas. `extract_server_url` resolves relative `servers[]` URLs against the spec's fetch URL. |
| [`mcp_discovery.py`](mcp_discovery.py) | Connect to an MCP server (SSE or stdio) **at import time** and return its tool list with input schemas. Honors `timeout_seconds`. Tool *execution* against MCP servers happens on the VM via the `mcp` capability, not here. |
| [`generators/`](generators/) | Tier-specific generators that turn wizard inputs into `(source_code, manifest, parameters_schema)`. See [`generators/README.md`](generators/README.md). |

## Tier semantics (UI-facing)

Tiers persist as a UI affordance — they decide which wizard the admin uses
and which detail view the admin sees. They no longer drive execution; every
tier produces a runtime-shape tool.

| Tier | Import flow | What the generator emits |
|---|---|---|
| `http_action` | Form (URL, method, headers, body template, credential) | `http.call(url, ...)` source + `net.hosts = [host]` (+ `secrets.names` if credential bound) |
| `openapi` | Wizard: paste spec → pick operations → name the tool | `openapi.call(cfg, op, args)` aggregated source + `net.hosts = [host]` |
| `mcp_server` | Wizard: discover server tools → pick operations → name the tool | `mcp.call(server, op, args)` aggregated source + `mcp.servers = [url]` (SSE only) |
| `typescript` | Admin pastes TS source + declares manifest | Passes source + manifest through unchanged |

## Security boundaries

- **No raw credentials in logs** — `credential.py` only logs IDs, never plaintext.
- **Credential plaintext stays in the runtime isolate** — fetched from `/internal/custom-tools/credentials/by-name/{n}` by the runtime's `secrets` capability; cached only for the duration of the call.
- **SSRF guard on every spec fetch** — `assert_safe_url` covers OpenAPI spec URL fetches done by AgentOS at import time.
- **Tool-execution SSRF** — enforced by the runtime's `net` capability allowlist (kernel-level enforcement is documented as a phase-5+ hardening option in [`documentation/CUSTOM_TOOLS.md`](../../../documentation/CUSTOM_TOOLS.md)).

## Related

- Router layer: [`app/routers/custom_tools/README.md`](../../routers/custom_tools/README.md)
- OpenClaw-side plugin: [`agent_os_openclaw/extensions/custom-tools/README.md`](../../../../agent_os_openclaw/extensions/custom-tools/README.md)
- Overview + sequence diagrams: [`documentation/CUSTOM_TOOLS.md`](../../../documentation/CUSTOM_TOOLS.md)
