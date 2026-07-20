# Custom Tools Plugin

OpenClaw plugin that exposes admin-defined tools (HTTP / OpenAPI / TypeScript /
MCP server) to agents at runtime — **without** requiring a gateway restart when
the admin adds, edits, or removes a tool.

Sister code on the control panel side: `agentos/app/routers/custom_tools/`
and `agentos/app/services/custom_tools/`.

## Files

| File | Purpose |
|---|---|
| [`index.ts`](index.ts) | Plugin entry. Registers `tools.reload` and `tools.test_execute` RPCs, schedules the 5-minute periodic refresh, provides the per-`chat.send` tool factory. Every tool is executed via the capability runtime in [`src/runtime/`](src/runtime/). |
| [`src/cache.ts`](src/cache.ts) | In-memory list of tool defs. `refreshCache` fetches `/internal/custom-tools` and replaces the list atomically. |
| [`src/types.ts`](src/types.ts) | `CustomToolDef` shape returned by `/internal/custom-tools`. |
| [`src/runtime/`](src/runtime/) | Capability runtime — isolate orchestration, capabilities (`net`, `storage`, `secrets`, `mcp`), SDK helpers (`http`, `openapi`), engine abstraction. See [`src/runtime/README.md`](src/runtime/README.md). |
| [`openclaw.plugin.json`](openclaw.plugin.json) | Plugin manifest (id, name, empty configSchema). |
| [`package.json`](package.json) | Plugin name + dependencies (currently `@modelcontextprotocol/sdk` for the `mcp` capability). |

## Lifecycle

```
gateway start
    │
    ▼
on("gateway_start") in index.ts
    ├─ getControlPanelConfig() — reads CONTROL_PANEL_URL + OPENCLAW_GATEWAY_TOKEN
    ├─ initAuditSink({ url, token }) — starts the runtime's audit forwarder.
    │     Every 5 s, batches up to 200 buffered capability events and POSTs
    │     them to /internal/custom-tools/audit. 2000-event ring buffer
    │     survives short AgentOS outages; oldest drop on overflow.
    ├─ refreshCache(url, token) — initial fetch from /internal/custom-tools
    └─ setInterval(refreshCache, 5 min) — self-heal in case a tools.reload is missed

chat.send arrives at the gateway
    │
    ▼
api.registerTool factory runs
    ├─ Reads getCachedTools() — zero I/O
    └─ Returns one entry per cached tool with an async execute() closure

agent calls a custom tool
    │
    ▼
execute(toolCallId, params)
    └─ runTool(def, params, { url, token, sessionKey })
         └─ runtime.executeTool(tool, args, ctx)  ← local V8 isolate, scoped capabilities
            Returns the tool's resolved value; plugin wraps as a text content item.

admin tests a tool from the AgentOS UI
    │
    ▼
gateway tools.test_execute RPC
    └─ runtime.executeTool(...)  ← same code path; agent_id and workspace_path are placeholders

admin saves a tool in AgentOS
    │
    ▼
gateway_admin.tools_reload() (WebSocket RPC)
    │
    ▼
registerGatewayMethod("tools.reload", ...) in index.ts
    └─ refreshCache(url, token) — cache replaced
       respond(true, { ok: true, count })

gateway stop
    │
    ▼
on("gateway_stop") — clearInterval the periodic refresh + stopAuditSink()
                     so the audit flush timer doesn't hold the process open
```

## Why no gateway restart is needed

- Tool **definitions** live in the control panel's PostgreSQL, not in plugin
  source. Adding a tool is a DB insert.
- The plugin **fetches them**, it doesn't bundle them. Source code of the
  plugin only changes when the *delivery mechanism* changes, not when tools do.
- `tools.reload` is the fast path (≤ 1 s after admin saves). Periodic refresh
  is the slow safety net (≤ 5 min). Either way the running gateway picks up
  the change.

## When the plugin itself changes

If you change `index.ts`, `cache.ts`, or `types.ts`:

1. Push to the OpenClaw setup repo.
2. Trigger the Cloud Build that streams source into the existing VM and runs
   `redeploy.sh` (`pnpm install` → `pnpm build` → `systemctl restart
   openclaw-gateway`). **Same VM** — terraform doesn't replace anything.

A full VM replace (terraform destroy + apply) is only needed if the
infra/terraform module changes (machine type, disk, startup-script template,
service-account binding). None of that is in scope for tool changes.

## Environment

| Variable | Source | Set by |
|---|---|---|
| `CONTROL_PANEL_URL` | injected env on VM | `start-openclaw.sh` (control panel's Cloud Run URL) |
| `OPENCLAW_GATEWAY_TOKEN` | Secret Manager → `/etc/openclaw/env` | terraform startup script |

If `CONTROL_PANEL_URL` is missing the plugin logs a warning and disables
itself — the gateway still starts, just without dynamic tools.

## Related docs

- Overview + sequence diagrams: [agentos — documentation/CUSTOM_TOOLS.md](https://github.com/Pilot-Wave-Technology/AgentOS/blob/main/documentation/CUSTOM_TOOLS.md)
- Control panel routes: [agentos — app/routers/custom_tools/README.md](https://github.com/Pilot-Wave-Technology/AgentOS/blob/main/app/routers/custom_tools/README.md)
- Control panel services: [agentos — app/services/custom_tools/README.md](https://github.com/Pilot-Wave-Technology/AgentOS/blob/main/app/services/custom_tools/README.md)
