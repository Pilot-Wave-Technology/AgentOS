# Tool Runtime — Phase 1

Capability-based execution for admin-defined custom tools, running inside the
OpenClaw plugin on the gateway VM.

## Why this exists

The control panel still owns the admin UI, the import wizards, the credential
store, and the unified `tools` table. But execution of TypeScript tool source
moves here, where:

- Tools can access the agent's workspace files via a scoped `storage` capability (the file system is right there)
- Network calls don't round-trip through Cloud Run
- The execution model is uniform across what used to be four separate "tier" executors — every tool is a TS function that uses capability primitives

What the tool author sees is a small set of globals injected into a fresh V8
context per call. No `process`, no `require`, no `Deno`, no native `fetch` —
only what the tool's manifest declared.

## Module layout (with the dependency-inversion seams)

```
                 ┌──────────────────────────┐
                 │  defaults.ts             │  ← composition root
                 │  (the ONE place that     │
                 │   imports concrete impls)│
                 └────────┬─────────────────┘
                          │
                          ▼
              ┌────────────────────────┐
              │  isolate.ts            │  orchestrator
              │  (depends on           │
              │   interfaces only)     │
              └────┬───────────────────┘
                   │
       ┌───────────┴───────────┬─────────────────┐
       ▼                       ▼                 ▼
┌──────────────┐       ┌───────────────┐   ┌──────────┐
│ engine.ts    │       │ factories.ts  │   │ audit.ts │
│ IsolateEngine│       │ CapabilityB.  │   │ types.ts │
│ (interface)  │       │ SdkBuilders   │   │          │
└──────┬───────┘       │ (interfaces)  │   └──────────┘
       │               └───────┬───────┘
       ▼                       ▼
┌──────────────────┐   ┌─────────────────────────┐
│ engines/         │   │ capabilities/ + sdk/    │
│   node-vm.ts     │   │   net.ts, storage.ts,   │
│ (swap point)     │   │   secrets.ts            │
└──────────────────┘   │   sdk/http.ts, openapi  │
                       │ (swap point)            │
                       └─────────────────────────┘
```

Dependency arrows point from concrete to abstract. `isolate.ts` knows nothing
about `node:vm` or any specific capability — those bindings are in `defaults.ts`.
Migrating to `isolated-vm` or `worker_threads` is a new file under `engines/`
plus a one-line change in `defaults.ts`.

## Files

| File | Purpose |
|---|---|
| [`isolate.ts`](isolate.ts) | `executeTool(tool, args, ctx, options?)` — orchestrator. Builds capabilities + SDK using the (overridable) builders, wraps the source, calls the engine. Depends only on `engine.ts` + `factories.ts` interfaces. |
| [`engine.ts`](engine.ts) | `IsolateEngine` interface — the abstraction over "run JS in a sandbox." Migration point for `isolated-vm` / `worker_threads` / WASM later. |
| [`factories.ts`](factories.ts) | `CapabilityBuilders` and `SdkBuilders` interfaces — the abstraction over "build a capability/helper from a grant + context." Migration point for alternate capability backends. |
| [`defaults.ts`](defaults.ts) | Composition root — the only file that imports the concrete capability, SDK, and engine modules. Exports `defaultBuilders` and `defaultEngine`. |
| [`audit.ts`](audit.ts) | `emit(ctx, event)` — structured JSON per capability call. Writes to stdout AND batches to AgentOS `/internal/custom-tools/audit` via `initAuditSink()` (called from the plugin's gateway_start). 2000-event ring buffer survives short AgentOS outages. |
| [`validate.ts`](validate.ts) | Minimal JSON-Schema validator (`type`, `properties`, `required`, `array.items`, `enum`). Injected as the `validate` global into every tool isolate — generators emit `validate(args.arguments, SCHEMAS[op])` for per-operation arg checking. |
| [`types.ts`](types.ts) | `ToolManifest`, `AgentCallContext`, `RuntimeTool`, `NetResponse`, grant types (`NetGrant`, `StorageGrant`, `SecretsGrant`, `McpGrant`). |
| [`engines/node-vm.ts`](engines/node-vm.ts) | Default `IsolateEngine` using Node's built-in `node:vm`. Trade-offs (memory not capped, async not terminable) documented at the top of the file. |
| [`engines/WORKER_THREADS_PLAN.md`](engines/WORKER_THREADS_PLAN.md) | Design + migration plan for swapping in a `worker_threads`-based engine that adds hard memory caps and async termination. Migration is one line in `defaults.ts`. |
| [`capabilities/net.ts`](capabilities/net.ts) | `net.fetch(url, init)` — host-allowlisted, scheme-restricted (`http:`/`https:`), returns a frozen plain `NetResponse`. |
| [`capabilities/storage.ts`](capabilities/storage.ts) | `storage.{list,read,stat}` always; `{write,mkdir,remove}` when manifest grants `mode: readwrite`. Paths resolved against the agent's workspace; escape is impossible by construction. |
| [`capabilities/secrets.ts`](capabilities/secrets.ts) | `secrets.get(name)` — fetches plaintext from AgentOS `/internal/custom-tools/credentials/by-name/{name}` over Bearer. Per-call memoisation; Fernet key never leaves Cloud Run. |
| [`capabilities/mcp.ts`](capabilities/mcp.ts) | `mcp.call(serverUrl, toolName, args, options?)` — server-allowlisted MCP invocation. `options.headers` are forwarded to both the SSE handshake and the message POSTs so a tool can pass per-call auth (`Authorization: Bearer …` / `X-API-Key`). Lazy-imports the MCP SDK; each call opens an SSE session and closes it after. |
| [`sdk/http.ts`](sdk/http.ts) | `http.call(url, opts)` — ergonomic wrapper over `net.fetch`. Auto JSON content-type, auto body stringify, auto response parse. Adds no privilege. |
| [`sdk/openapi.ts`](sdk/openapi.ts) | `openapi.call(cfg, opId, args)` — operation dispatcher used by wizard-generated OpenAPI tools. Path param substitution + query/body routing. Adds no privilege. |

## Tool author contract

A tool is a TypeScript file that defines an async `execute` function:

```ts
async function execute(args) {
  // Globals injected by the runtime:
  //   net, storage, secrets, mcp     — capabilities, iff the manifest grants them
  //   http, openapi                  — SDK helpers over `net` (auto-injected when net is granted)
  //   validate(value, schema, path?) — minimal JSON-Schema validator; always available
  //   console.{log,warn,error}       — routed through the audit pipe
  // Calling a global the manifest didn't grant throws ReferenceError.
  const data = await net.fetch("https://api.example.com/things");
  return data.json();
}
```

`export default async function execute` is also accepted — the runtime strips
the `export default` since `vm.runInContext` evaluates as a script, not a module.

Manifest declares what the tool can do:

```yaml
capabilities:
  net:
    hosts: ["api.example.com"]
  storage:
    mode: read              # or readwrite
  secrets:
    names: ["api_example_token"]
limits:
  timeout_ms: 30000         # default 30s, hard cap 120s
```

Any capability not in the manifest is **not injected as a global** — the tool
code attempting to reference `storage` (when not granted) gets a `ReferenceError`.

## What's enforced where

| Concern | Where it's enforced | How |
|---|---|---|
| Network destination | `capabilities/net.ts` | Hostname checked against `grant.hosts` before any HTTP is initiated |
| URL scheme | `capabilities/net.ts` | Only `http:` / `https:` allowed (rejects `file:`, `data:`, etc.) |
| Filesystem path escape | `capabilities/storage.ts` | Every path is resolved to an absolute path; rejected if not under `workspace_path` |
| Write/delete on read-mode tools | `capabilities/storage.ts` | Read-mode returns a capability with only `{list,read,stat}` — `write` literally doesn't exist on the object |
| Secret access | `capabilities/secrets.ts` | Name must be in `grant.names`; allowlist enforced before any fetch |
| Sync code timeout | `isolate.ts` | `vm.runInContext({ timeout })` |
| Async code timeout | `isolate.ts` | `Promise.race` against `setTimeout` (best-effort — see "Known limits") |
| String code generation (`eval`, `new Function`) | `isolate.ts` | `codeGeneration: { strings: false, wasm: false }` |
| Audit trail | `audit.ts` | Every capability call emits a structured event with outcome=ok/denied |

## What's NOT enforced (yet)

- **Memory limit** — `node:vm` shares the parent heap. A tool that allocates 4 GB will pressure the gateway. Hard memory caps require `isolated-vm` (third-party native module) or moving execution to a `worker_threads` boundary. Deferred to a later phase.
- **Async termination** — if a tool exceeds its async timeout, in-flight work continues in the background until it resolves on its own. The audit log captures this. Hard termination requires `isolated-vm` or `worker_threads`.
- **CPU quota** — same story as memory. Needs `worker_threads` + per-worker resource limits.

These are acceptable phase-1 trade-offs because:
1. Tool source is admin-reviewed before being granted capabilities
2. The audit log catches anything weird
3. Gateway-level resource ceilings are still set at the systemd unit

## How a call flows

```
plugin receives executeTool() invocation
   │
   ▼
isolate.executeTool(tool, args, ctx)
   ├─ build capabilities from manifest    (only granted ones)
   ├─ vm.createContext({ args, console, net?, storage?, secrets? })
   ├─ wrap source: strip "export default", append `__tool_result = (async ()=>execute(args))()`
   ├─ vm.runInContext(wrapped, { timeout: ... })           ← sync code runs here
   └─ await Promise.race([__tool_result, timeoutPromise])   ← async code completes here
        ▲
        │
   each capability call emits an audit event
   each capability validates against the manifest grants
```

## What's NOT in this phase

- Wiring this into the plugin's `registerTool` factory (Phase 2 — plugin needs to know which tools are "runtime tools" via the new `manifest` column)
- The SDK library modules (`@pwt/sdk/http`, `@pwt/sdk/openapi`, `@pwt/sdk/mcp`) that wizards will generate against (Phase 2)
- Wizard generators (Phase 3)
- Migration of existing tier rows (Phase 4)

This phase delivers a runtime that can execute a hand-written TS tool with a
hand-written manifest, end-to-end. That's the unit on which the rest of the
plan builds.
