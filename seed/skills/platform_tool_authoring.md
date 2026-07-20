---
name: platform_tool_authoring
title: Authoring a Custom Tool
description: Use when a story asks you to add a tool (apply_add_tool). Explains the 4 tiers, how to research the target API, the exact spec per tier, and credential scaffolding.
applicable_to: PlatformAssistant
status: active
version: 2
---
# Authoring a Custom Tool

Adding a tool is NOT one-shot — you research the target service, choose the right
tier, build the tier-specific `spec`, then call `apply_add_tool`. Never guess a
URL, endpoint, method, or auth scheme.

## Step 1 — Research first (use your read / web_search / web_fetch tools)
Before building anything, establish:
- **What** the tool should do and the exact **operation(s)** it must expose.
- The service's **base URL** and the concrete **endpoint(s)** (method + path).
- The **request** shape (path/query params, JSON body) and the **response** shape.
- The **auth** scheme: none, Bearer token, an API key in a named header, or Basic.

Use `web_search`/`web_fetch` to read the provider's API docs; `read` for any spec
files already in the platform. Write down the endpoint + auth before continuing.

## Step 2 — Pick the tier
`apply_add_tool(tier, name, label, description, spec, credential?)`. Choose:

| Tier | Use when | Key `spec` fields |
|------|----------|-------------------|
| `http_action` | ONE HTTP endpoint (the common case) | `url`, `method`, `headers`, `body_template`, `parameters_schema` |
| `openapi` | The service has an OpenAPI spec and you want several operations | `server_url`, `operations[]` |
| `mcp_server` | The capability is an MCP server | `server_url`, `transport`, `tools[]` |
| `typescript` | Custom logic / multi-step glue (Deno sandbox; network is host-allowlisted via `manifest`) | `source_code`, `parameters_schema`, `manifest` |

Prefer `http_action` for a single endpoint; `openapi` when you genuinely need
multiple operations of one API.

## Step 3 — Build `spec` for the chosen tier

**http_action** — one call. `parameters_schema` is the JSON Schema of the args the
agent passes at call time. Interpolate those args into `url` / `headers` /
`body_template` using **`{{double-brace}}`** placeholders — single braces are NOT
substituted. For GET, put params in the query string of `url`:
```
spec = {
  "url": "https://api.example.com/v1/search?q={{query}}&limit={{limit}}",
  "method": "GET",
  "headers": {"Accept": "application/json"},
  "parameters_schema": {
    "type": "object",
    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["query"]
  }
}
```
For POST/PUT send a `body_template` instead (its string values also use
`{{placeholders}}`): `"body_template": {"query": "{{query}}"}`.

**openapi** — resolve the concrete operations yourself from the spec/docs (the
`{id}`-style path params are standard OpenAPI path templating, handled for you):
```
spec = {
  "server_url": "https://api.example.com",
  "operations": [
    {"operation_id": "searchItems", "method": "GET",  "path": "/v1/items",      "description": "Search items"},
    {"operation_id": "getItem",     "method": "GET",  "path": "/v1/items/{id}", "description": "Get one item"}
  ]
}
```

**mcp_server** — list the server's tool names (a plain list of strings is fine):
```
spec = {"server_url": "https://mcp.example.com/sse", "transport": "sse",
        "tools": ["search", "fetch"]}
```

**typescript** — supply source that defines `async function execute(args)`. Any
host you `fetch()` MUST be listed in `manifest.net.hosts` — the Deno sandbox blocks
every host not granted, so a tool with no `manifest` cannot reach the network:
```
spec = {
  "source_code": "async function execute(args){ const r = await fetch('https://api.example.com/x?q='+encodeURIComponent(args.q)); return await r.json(); }",
  "parameters_schema": {"type":"object","properties":{"q":{"type":"string"}},"required":["q"]},
  "manifest": {"net": {"hosts": ["api.example.com"]}}
}
```

## Step 4 — Credentials (you NEVER handle the secret)
If the endpoint needs auth, add a `credential` object. A **pending** credential
shell is scaffolded and wired to the tool; a human enters the secret in
**Settings → Credentials** before the tool can run.
```
credential = {"name": "example-api-key", "type": "api_key", "header_name": "X-API-Key"}
```
- `type` is one of `bearer`, `api_key`, `basic`. `api_key` REQUIRES `header_name`.
- For `http_action`/`openapi`/`mcp_server` the generator wires the credential into
  the request automatically — do NOT also hardcode a token in `headers`.
- After adding, tell the admin the exact credential name and that they must fill
  its secret; the tool stays inert (pending) until they do.

## Step 5 — Call it and verify
Call `apply_add_tool(...)`. On `{"ok": false, "reason": ...}` read the reason, fix
the `spec`, and retry — a failed attempt rolls back any scaffolded credential, so a
clean retry is safe. On `{"ok": true, ...}` the tool is live (a `tools.reload`
already fired); if the response `note` says a credential is pending, relay that to
the admin. Then update the story.

## Grounding
Only build what the story specifies. If you can't determine the endpoint, method,
or auth from research, ask the admin rather than guessing.