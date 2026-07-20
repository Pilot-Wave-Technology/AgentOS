# Worker-threads engine — design + migration plan

The default engine today is `node:vm` (see [`node-vm.ts`](node-vm.ts)). It runs
the tool in the gateway's main process — same heap, same event loop. Good
enough for trusted admin-curated tools at our current scale, but with two real
gaps that the capability boundary alone can't close:

1. **Memory cap.** A tool that allocates 4 GB inside its isolate pressures the
   gateway's V8 heap until the whole process dies. The capability list doesn't
   prevent this — `Array(1e9).fill({})` uses no granted capability.
2. **Async termination.** When the async timeout fires, in-flight async work
   keeps running in the background until it resolves naturally. Promise.race
   lets us return on time, but the orphan never gets cancelled.

A `worker_threads`-based engine fixes both: a separate event loop per call,
killable via `Worker.terminate()`, and `resourceLimits` enforces a hard heap
cap that the kernel/V8 enforces — not a polling check.

This file documents how to do that swap without rewriting capabilities or SDK
modules.

## What stays the same

Everything except the engine. The `IsolateEngine` contract in
[`../engine.ts`](../engine.ts) is engine-agnostic — it takes a wrapped source
string and a context-globals object, runs the script, and returns the promise
on `__tool_result`. Capability objects and SDK helpers, the manifest format,
audit emit, the `validate` helper — all unchanged.

`defaults.ts` becomes the single migration point:

```ts
// Before
export const defaultEngine: IsolateEngine = makeNodeVmEngine();
// After
export const defaultEngine: IsolateEngine = makeWorkerThreadEngine();
```

## What needs to change

Capability objects today are **functions on the main thread** — they close over
audit state, the fetch implementation, the credential cache. Workers can't
share function references with their parent thread directly. The workaround
is the standard `MessagePort` proxy pattern:

```
main thread                              worker thread
─────────────                            ─────────────
capabilities = { net.fetch: realFn }     proxies = {
        │                                  net: {
        │  setup: send port over            fetch: async (...args) => {
        ▼                                     postMessage({ kind: "net.fetch", args });
   spawn Worker  ──────port──────▶           return await awaitReply();
        │                                   }
        │                                  }
        │                                }
        │                                tool code calls proxies.net.fetch(...)
        │                                  │
        ▼                                  ▼
   onMessage handler                      postMessage to port
        │                                  ▲
        │  net.fetch(...args) ─────────────┘
        ▼
   real fetch happens here
        │
        │  result ─────────port──────▶  resolves the awaiting promise
```

Roughly 300–400 lines of code. The shape:

### Files to add

- `engines/worker-thread.ts` — `makeWorkerThreadEngine(): IsolateEngine` that:
  - Spawns a `Worker` with the tool source, resource limits, args
  - Wires a MessagePort for capability calls
  - Awaits the worker's exit; returns the captured result
- `engines/worker-thread.harness.ts` — code that runs **inside** the worker:
  - Receives source + manifest + port via `workerData`
  - Constructs *proxy* objects for net / storage / secrets / mcp / http / openapi
  - Each proxy method posts a message to the port and awaits the reply
  - Compiles + runs the user source in a `vm.runInContext` with the proxies as globals
  - Posts the resolved result back, then exits

### Resource limits

Per-call `resourceLimits` on the Worker constructor:

```ts
new Worker(harnessPath, {
  workerData: { source, manifest, args },
  resourceLimits: {
    maxOldGenerationSizeMb: limits.memory_mb ?? 64,
    maxYoungGenerationSizeMb: 16,
    codeRangeSizeMb: 8,
  },
});
```

A timeout enforced from the main thread via `setTimeout(() => worker.terminate(), N)`
gives hard kill. The orphan-async problem disappears — `terminate()` rips the
event loop down regardless of what's pending.

### Capability proxy contract

Each capability the manifest grants gets a proxy in the worker. The proxy's
methods serialise their arguments, post via the MessagePort, and wait for a
matching reply. The reply path on the main thread re-uses the existing
capability factories — same allowlist enforcement, same audit emit. From the
worker's perspective nothing changes: `await net.fetch(url, init)` works the
same way it does today.

Things that must NOT round-trip per call:
- `console.log` — buffer worker-side, flush via one message on exit
- Frequent `validate()` checks — the validator runs entirely in the worker
- `args` access — passed via `workerData`, no proxy needed

Things that DO round-trip:
- `net.fetch`, `storage.list/read/write`, `secrets.get`, `mcp.call`

Per-call overhead: one message-send for each capability invocation. Compared
to network I/O latency of an actual `fetch`, this is negligible.

### Worker startup cost

A fresh worker per call costs ~10–20 ms of cold start. For interactive agent
loops that's acceptable. Two optimisations available if it becomes a concern:

1. **Worker pool** with code reload — keep N idle workers, send them
   `{ source, args }` per call, reuse the worker after it returns the result.
   Requires per-call source compilation inside the worker but skips Worker
   spin-up.
2. **Per-tool worker** — keep one worker per (cached) tool source; calls
   re-use the same compiled script. Higher memory baseline, near-zero
   per-call overhead.

Start with fresh-per-call. Optimise if/when load demands.

## Migration steps

1. Add `engines/worker-thread.ts` + `engines/worker-thread.harness.ts`.
2. Local smoke test: a tool that allocates 200 MB then sleeps for 2 min —
   verify it dies cleanly at the memory cap and at the timeout.
3. Behind a `RUNTIME_ENGINE=worker_threads` env var, swap engines in
   `defaults.ts`:
   ```ts
   export const defaultEngine: IsolateEngine =
     process.env.RUNTIME_ENGINE === "worker_threads"
       ? makeWorkerThreadEngine()
       : makeNodeVmEngine();
   ```
4. Test in feature env via the env-var override. Watch the audit log for
   regressions.
5. Flip the default to `makeWorkerThreadEngine()` once stable.
6. Optionally delete `engines/node-vm.ts` — at that point it's only useful as
   a reference implementation.

Throughout: capability files, SDK files, generators, AgentOS endpoints all
stay the same. The DI seam earns its keep here.

## What this doesn't fix

- A tool can still consume gateway CPU during synchronous code (V8 doesn't
  pause; the worker just gets killed by the timeout). For CPU-bound tools
  consider `worker_threads` + a separate CPU-time monitor.
- Workers share the gateway's filesystem permissions (same Unix user). Hard
  filesystem isolation needs `unshare` / namespaces / containers, separate
  from the engine choice.

These are non-issues for trusted admin tools and don't need solving until the
threat model changes.
