# Tool source generators

Each of the four import wizards (HTTP action / OpenAPI / TypeScript / MCP)
turns its wizard-shaped inputs into a `(source_code, manifest, parameters_schema)`
triple that the capability runtime in the OpenClaw plugin can execute.

## Files

| File | Tier | Purpose |
|---|---|---|
| [`__init__.py`](__init__.py) | — | `ToolGenerator` Protocol, `register(tier, gen)`, `generate(tier, inputs)`, registry |
| [`http_action.py`](http_action.py) | `http_action` | Builds a `http.call(url, ...)` source from form inputs; declares `net.hosts = [host]` (+ `secrets.names` and `secrets.get` + auth-header injection if credential bound) |
| [`openapi.py`](openapi.py) | `openapi` | Builds `openapi.call(...)` aggregated tool source from selected operations. Embeds per-operation argument schemas into a `SCHEMAS` map and emits `validate(args.arguments, SCHEMAS[op])` at the top of the generated execute() — the runtime's `validate` global throws a clear "missing required X / wrong type" before the upstream call fires |
| [`mcp_server.py`](mcp_server.py) | `mcp_server` | Builds `mcp.call(SERVER, op, args[, {headers}])` aggregated tool; declares `mcp.servers = [url]`. When a credential is bound, also emits `secrets.get` + the appropriate header (Bearer / api_key / Basic) and adds `secrets.names` to the manifest. Embeds per-tool `input_schema` and emits the same `validate()` pre-dispatch check as openapi. SSE transport only |
| [`typescript.py`](typescript.py) | `typescript` | Passes through admin source + admin-declared manifest. No synthesis. Admin declares capabilities via the structured form in `typescript_form.html`. |

## Why a registry (loose-coupling rule)

Wizard handlers depend on the abstraction (`generate(tier, inputs)`), not on
the concrete tier modules. Adding a new wizard later — say `graphql` — means:

1. Add `generators/graphql.py` with one `_generate(inputs)` function
2. Call `register("graphql", _generate)` at module bottom
3. Done — every wizard handler that goes through `api_create_tool` (or has its
   own endpoint that calls `generate("graphql", ...)`) just works

No changes to `api_create_tool`, no changes to the runtime, no changes to the
CRUD layer. The dependency arrows point from wizard handlers → registry →
generators, never the reverse.

## Contract

Every generator returns the same `GeneratedTool` shape:

```python
{
    "source_code": "...TS that defines `async function execute(args)`...",
    "manifest":    {"capabilities": {"net": {"hosts": [...]}, ...}, "limits": {...}},
    "parameters_schema": {"type": "object", "properties": {...}, "required": [...]},
}
```

Generators are **pure transforms** — no DB access, no env reads, no I/O.
Wizard handlers do credential lookups and pass `{name, type, header_name?}`
into the generator inputs. Generators never see `credential_id`.

## Call sites

| Wizard | HTTP endpoint | How it calls the generator |
|---|---|---|
| HTTP action | `POST /api/custom-tools` (tier=http_action) | via `api_create_tool` → tier dispatch |
| TypeScript  | `POST /api/custom-tools` (tier=typescript)  | via `api_create_tool` → tier dispatch |
| OpenAPI     | `POST /api/custom-tools/openapi/generate`   | `api_openapi.api_generate_openapi_tools` calls `generate("openapi", ...)` directly |
| MCP         | `POST /api/custom-tools/mcp/generate`       | `api_mcp.api_mcp_generate` calls `generate("mcp_server", ...)` directly |

The OpenAPI and MCP wizards have dedicated endpoints because their pre-generate
step involves more than building source — they store a spec or perform server
discovery first. That state lives in their endpoints; the actual source/manifest
generation is delegated through the registry.
