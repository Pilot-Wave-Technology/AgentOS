# Insights Plugin

Read-only gateway RPCs for the AgentOS control-panel dashboard — sandbox/container runtime health today, with room to grow into other platform metrics.

Zero changes to OpenClaw core. It depends on OpenClaw's internal APIs (fine — same repo), but core never imports from this plugin, and this plugin never imports from the control panel. All coupling flows through the stable RPC interface (method name + JSON payload shape).

## Files

| File | Purpose |
|---|---|
| [`index.ts`](index.ts) | The whole plugin — registers `sandbox.status`, no other files |

## Enabling

```bash
node openclaw.mjs config set plugins.entries.insights.enabled true
```

## `sandbox.status`

Reports whether the sandbox runtime is configured, reachable, and how many sandbox containers are currently active. Never fails the request — on any internal error it responds `ok=true` with `mode: "unknown"` and an `error` field, so a broken sandbox check never surfaces as a broken dashboard.

Response shape:

```
{
  mode:            "off" | "all" | "non-main" | string,   // configured sandbox mode
  enabled:         boolean,   // mode !== "off"
  runtime:         "podman" | "docker" | null,             // detected container runtime
  runtime_ok:      boolean,   // runtime CLI reachable
  active:          number,    // running sandbox containers (matched by name prefix)
  image:           string,    // configured sandbox image name
  image_available: boolean,   // image present locally
}
```

Detection logic: tries `podman --version` first, then `docker --version`; whichever responds becomes the detected runtime. Active container count comes from `<runtime> ps --filter name=<prefix>`, where the prefix is the configured sandbox container prefix (or `DEFAULT_SANDBOX_CONTAINER_PREFIX` if unset).

## Related docs

- Consumed by the AgentOS dashboard — see [`agent_os_platform` — app/services/gateway_cache.py](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/app/services/gateway_cache.py) (caches the RPC response so the dashboard doesn't call the gateway on every page load)
- [ARCHITECTURE.md](../../ARCHITECTURE.md#gateway-side-extensions) for how this fits alongside the other gateway extensions
