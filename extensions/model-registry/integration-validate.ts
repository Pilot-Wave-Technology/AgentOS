/**
 * Integration validation driver for the model-registry plugin.
 *
 * This is NOT a vitest test file — it's a runnable Node/tsx script that
 * exercises the plugin against real components:
 *   - Real HTTP fixture server (http.createServer on 127.0.0.1:0).
 *   - Real cache module (fetch → assertCatalogShape → module-scope cache).
 *   - Real plugin.register() that wires the resolve_providers hook and the
 *     models.reload gateway RPC.
 *   - Real PluginRegistry + setActivePluginRegistry.
 *   - Real ensureOpenClawModelsJson pipeline (planOpenClawModelsJson,
 *     runPluginResolveProviders, mergeProviders, normalizeProviders,
 *     enforceSourceManagedProviderSecrets, writeModelsFileAtomic).
 *   - Real temp directory + real on-disk models.json.
 *
 * Run:  pnpm exec tsx extensions/model-registry/integration-validate.ts
 *
 * Exits 0 on all scenarios passing, 1 on any failure.
 */
import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { AddressInfo } from "node:net";

import { ensureOpenClawModelsJson } from "../../src/agents/models-config.js";
import type { OpenClawConfig } from "../../src/config/config.js";
import { createEmptyPluginRegistry, type PluginRegistry } from "../../src/plugins/registry.js";
import { setActivePluginRegistry } from "../../src/plugins/runtime.js";
import type {
  OpenClawPluginApi,
  PluginHookHandlerMap,
  PluginHookRegistration,
} from "../../src/plugins/types.js";

import modelRegistryPlugin from "./index.js";
import { clearCache } from "./src/cache.js";

// ── Types for the harness ────────────────────────────────────────────────

interface CapturedHooks {
  gateway_start?: PluginHookHandlerMap["gateway_start"];
  gateway_stop?: PluginHookHandlerMap["gateway_stop"];
}

interface Harness {
  registry: PluginRegistry;
  hooks: CapturedHooks;
  logs: { level: "debug" | "info" | "warn" | "error"; msg: string }[];
}

function mkApi(pluginId: string, harness: Harness): OpenClawPluginApi {
  return {
    id: pluginId,
    name: pluginId,
    // Typed lifecycle hooks: resolve_providers goes into the registry
    // (so runResolveProviders sees it); gateway_{start,stop} are captured
    // locally so the harness can invoke them explicitly.
    on: <K extends keyof PluginHookHandlerMap>(
      hookName: K,
      handler: PluginHookHandlerMap[K],
    ) => {
      if (hookName === "gateway_start") {
        harness.hooks.gateway_start = handler as PluginHookHandlerMap["gateway_start"];
        return;
      }
      if (hookName === "gateway_stop") {
        harness.hooks.gateway_stop = handler as PluginHookHandlerMap["gateway_stop"];
        return;
      }
      const registration: PluginHookRegistration = {
        pluginId,
        hookName,
        handler,
        source: "integration-validate",
      };
      harness.registry.typedHooks.push(registration);
    },
    registerGatewayMethod: (method, handler) => {
      harness.registry.gatewayHandlers[method] = handler;
    },
    logger: {
      debug: (msg: string) => harness.logs.push({ level: "debug", msg }),
      info: (msg: string) => harness.logs.push({ level: "info", msg }),
      warn: (msg: string) => harness.logs.push({ level: "warn", msg }),
      error: (msg: string) => harness.logs.push({ level: "error", msg }),
    },
    // Fields the plugin never touches — cast keeps the harness minimal.
  } as unknown as OpenClawPluginApi;
}

function mkHarness(): Harness {
  return {
    registry: createEmptyPluginRegistry(),
    hooks: {},
    logs: [],
  };
}

// ── Fixture HTTP server ──────────────────────────────────────────────────

interface FixtureServer {
  url: string;
  requests: { url: string; headers: Record<string, string | undefined> }[];
  close: () => Promise<void>;
}

async function startFixtureServer(
  responder: (req: IncomingMessage, res: ServerResponse) => void,
): Promise<FixtureServer> {
  const requests: FixtureServer["requests"] = [];
  const srv = createServer((req, res) => {
    requests.push({
      url: req.url ?? "",
      headers: {
        authorization:
          typeof req.headers.authorization === "string" ? req.headers.authorization : undefined,
      },
    });
    responder(req, res);
  });
  await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", () => resolve()));
  const addr = srv.address() as AddressInfo;
  const url = `http://127.0.0.1:${addr.port}`;
  return {
    url,
    requests,
    close: () =>
      new Promise<void>((resolve, reject) =>
        srv.close((err) => (err ? reject(err) : resolve())),
      ),
  };
}

// ── Env helpers ──────────────────────────────────────────────────────────

const ENV_KEYS = ["CONTROL_PANEL_URL", "OPENCLAW_GATEWAY_TOKEN", "REGISTRY_KEY"] as const;
type EnvKey = (typeof ENV_KEYS)[number];

function setEnv(overrides: Partial<Record<EnvKey, string | undefined>>) {
  for (const k of ENV_KEYS) {
    if (overrides[k] === undefined) {
      delete process.env[k];
    } else {
      process.env[k] = overrides[k];
    }
  }
}

async function mkTempAgentDir(scenario: string): Promise<string> {
  return await fs.mkdtemp(path.join(os.tmpdir(), `mr-int-${scenario}-`));
}

async function rmTempAgentDir(dir: string) {
  await fs.rm(dir, { recursive: true, force: true });
}

// ── Scenario runners ─────────────────────────────────────────────────────

function baseConfig(): OpenClawConfig {
  // Minimal config: no explicit providers, no channels. resolveImplicitProviders
  // returns {} on a bare process with no env-var-driven providers.
  return {} as OpenClawConfig;
}

function configWithControlProvider(): OpenClawConfig {
  // Scenarios (b) and (c) need something in models.providers so the pipeline
  // actually writes a file — otherwise planOpenClawModelsJson short-circuits
  // to `skip` when the merged provider map is empty and the byte-identical
  // comparison becomes trivial ("neither file exists").
  return {
    models: {
      providers: {
        "control-provider": {
          baseUrl: "https://control.example",
          apiKey: "CONTROL_KEY",
          models: [{ id: "control-model" }],
        },
      },
    },
  } as unknown as OpenClawConfig;
}

const CATALOG_BODY = {
  providers: {
    "registry-provider": {
      baseUrl: "https://example.test",
      apiKey: "REGISTRY_KEY",
      models: [{ id: "registry-model" }],
    },
  },
};

interface ScenarioResult {
  name: string;
  passed: boolean;
  evidence: string[];
  errors: string[];
}

async function runScenarioA(): Promise<ScenarioResult> {
  const result: ScenarioResult = {
    name: "(a) happy path — plugin loaded, catalog fetched, models.json contains registry-provider",
    passed: false,
    evidence: [],
    errors: [],
  };
  const server = await startFixtureServer((req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(CATALOG_BODY));
  });
  const agentDir = await mkTempAgentDir("a");
  const harness = mkHarness();
  try {
    setEnv({
      CONTROL_PANEL_URL: server.url,
      OPENCLAW_GATEWAY_TOKEN: "test-token",
      REGISTRY_KEY: "sk-fake",
    });
    setActivePluginRegistry(harness.registry, "integration-validate-a");
    modelRegistryPlugin.register(mkApi("model-registry", harness));

    assert.ok(harness.hooks.gateway_start, "plugin did not register gateway_start");
    // Invoke gateway_start — this triggers the initial fetch that populates the cache.
    await harness.hooks.gateway_start!(
      { port: 0 },
      { port: 0 },
    );

    // Now drive the real models.json write pipeline.
    const outcome = await ensureOpenClawModelsJson(baseConfig(), agentDir);
    result.evidence.push(
      `ensureOpenClawModelsJson returned wrote=${outcome.wrote} agentDir=${outcome.agentDir}`,
    );

    const modelsJsonPath = path.join(agentDir, "models.json");
    const contents = await fs.readFile(modelsJsonPath, "utf8");
    const parsed = JSON.parse(contents) as {
      providers?: Record<string, { baseUrl?: string; apiKey?: string; models?: { id?: string }[] }>;
    };

    assert.equal(
      parsed.providers?.["registry-provider"]?.baseUrl,
      "https://example.test",
      "baseUrl mismatch",
    );
    assert.equal(
      parsed.providers?.["registry-provider"]?.apiKey,
      "REGISTRY_KEY",
      "apiKey mismatch (expected env-var marker, not plaintext)",
    );
    assert.equal(
      parsed.providers?.["registry-provider"]?.models?.[0]?.id,
      "registry-model",
      "models[0].id mismatch",
    );
    assert.ok(
      !contents.includes("sk-fake"),
      "plaintext 'sk-fake' leaked into models.json on disk",
    );

    // Server-side observation: exactly one request to /internal/model_catalog with bearer.
    assert.equal(server.requests.length, 1, `expected 1 request, got ${server.requests.length}`);
    assert.equal(server.requests[0].url, "/internal/model_catalog");
    assert.equal(
      server.requests[0].headers.authorization,
      "Bearer test-token",
      `Authorization header mismatch: ${server.requests[0].headers.authorization}`,
    );

    result.evidence.push(
      `models.json providers["registry-provider"] = ${JSON.stringify(
        parsed.providers?.["registry-provider"],
      )}`,
    );
    result.evidence.push(`fixture server saw ${server.requests.length} request; auth header OK`);
    result.evidence.push(
      `no plaintext 'sk-fake' in ${contents.length} bytes of on-disk models.json`,
    );
    result.passed = true;
  } catch (err) {
    result.errors.push(String(err));
  } finally {
    // Cleanup: cache, timers, env, tempdir, server.
    if (harness.hooks.gateway_stop) {
      harness.hooks.gateway_stop({ reason: "test-cleanup" }, { port: 0 });
    }
    clearCache();
    setEnv({});
    await server.close();
    await rmTempAgentDir(agentDir);
  }
  return result;
}

async function runControlNoPlugin(
  cfg: OpenClawConfig,
  agentDir: string,
): Promise<string> {
  // Reference run: brand-new empty registry, no plugin, same config.
  setActivePluginRegistry(createEmptyPluginRegistry(), "integration-validate-control");
  await ensureOpenClawModelsJson(cfg, agentDir);
  return await fs.readFile(path.join(agentDir, "models.json"), "utf8");
}

async function runScenarioB(): Promise<ScenarioResult> {
  const result: ScenarioResult = {
    name:
      "(b) disabled path — CONTROL_PANEL_URL unset produces models.json byte-identical to control run",
    passed: false,
    evidence: [],
    errors: [],
  };
  const agentDirPlugin = await mkTempAgentDir("b-plugin");
  const agentDirControl = await mkTempAgentDir("b-control");
  const harness = mkHarness();
  try {
    setEnv({
      // CONTROL_PANEL_URL intentionally unset
      OPENCLAW_GATEWAY_TOKEN: "test-token",
      REGISTRY_KEY: "sk-fake",
    });
    setActivePluginRegistry(harness.registry, "integration-validate-b-plugin");
    modelRegistryPlugin.register(mkApi("model-registry", harness));
    await harness.hooks.gateway_start!({ port: 0 }, { port: 0 });

    const cfg = configWithControlProvider();
    await ensureOpenClawModelsJson(cfg, agentDirPlugin);
    const pluginContents = await fs.readFile(
      path.join(agentDirPlugin, "models.json"),
      "utf8",
    );

    // Second run: no plugin at all. Same env (CONTROL_PANEL_URL still unset).
    const controlContents = await runControlNoPlugin(cfg, agentDirControl);

    assert.equal(
      pluginContents,
      controlContents,
      `models.json byte-mismatch (plugin=${pluginContents.length} bytes, control=${controlContents.length} bytes)`,
    );
    // Sanity: the file actually has content (control-provider present) so the
    // byte-comparison is non-trivial.
    assert.ok(
      pluginContents.includes("control-provider"),
      "control-provider missing from pluginContents — comparison would be meaningless",
    );

    const warnLogs = harness.logs.filter((l) => l.level === "warn");
    assert.ok(
      warnLogs.some((l) => /CONTROL_PANEL_URL not set/.test(l.msg)),
      `expected a 'CONTROL_PANEL_URL not set' warn log, got: ${JSON.stringify(harness.logs)}`,
    );

    result.evidence.push(
      `plugin-loaded models.json (${pluginContents.length} bytes) === control models.json (${controlContents.length} bytes)`,
    );
    result.evidence.push(
      `plugin logged 'CONTROL_PANEL_URL not set — model registry disabled' warning as expected`,
    );
    result.passed = true;
  } catch (err) {
    result.errors.push(String(err));
  } finally {
    if (harness.hooks.gateway_stop) {
      harness.hooks.gateway_stop({ reason: "test-cleanup" }, { port: 0 });
    }
    clearCache();
    setEnv({});
    await rmTempAgentDir(agentDirPlugin);
    await rmTempAgentDir(agentDirControl);
  }
  return result;
}

async function runScenarioC(): Promise<ScenarioResult> {
  const result: ScenarioResult = {
    name: "(c) failure path — fixture server returns HTTP 500, models.json written without registry-provider",
    passed: false,
    evidence: [],
    errors: [],
  };
  const server = await startFixtureServer((_req, res) => {
    res.writeHead(500, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "internal" }));
  });
  const agentDirPlugin = await mkTempAgentDir("c-plugin");
  const agentDirControl = await mkTempAgentDir("c-control");
  const harness = mkHarness();
  try {
    setEnv({
      CONTROL_PANEL_URL: server.url,
      OPENCLAW_GATEWAY_TOKEN: "test-token",
      REGISTRY_KEY: "sk-fake",
    });
    setActivePluginRegistry(harness.registry, "integration-validate-c-plugin");
    modelRegistryPlugin.register(mkApi("model-registry", harness));
    await harness.hooks.gateway_start!({ port: 0 }, { port: 0 });

    const cfg = configWithControlProvider();
    await ensureOpenClawModelsJson(cfg, agentDirPlugin);
    const pluginContents = await fs.readFile(
      path.join(agentDirPlugin, "models.json"),
      "utf8",
    );

    // Control run: no plugin loaded; CONTROL_PANEL_URL still points at 500 server
    // (irrelevant — nothing consumes it).
    const controlContents = await runControlNoPlugin(cfg, agentDirControl);

    // registry-provider must NOT appear on disk.
    assert.ok(
      !pluginContents.includes("registry-provider"),
      `registry-provider unexpectedly appeared in models.json after HTTP 500 fetch`,
    );
    assert.equal(
      pluginContents,
      controlContents,
      `models.json byte-mismatch on failure path (plugin=${pluginContents.length}, control=${controlContents.length})`,
    );
    assert.ok(
      pluginContents.includes("control-provider"),
      "control-provider missing — comparison would be meaningless",
    );

    const warnLogs = harness.logs.filter((l) => l.level === "warn");
    assert.ok(
      warnLogs.some((l) => /initial cache load failed.*HTTP 500/i.test(l.msg)),
      `expected 'initial cache load failed — HTTP 500' warn log, got: ${JSON.stringify(
        harness.logs,
      )}`,
    );

    result.evidence.push(
      `on-disk models.json (${pluginContents.length} bytes) matches control run; no 'registry-provider' entry`,
    );
    result.evidence.push(
      `plugin logged warn: 'model-registry: initial cache load failed — Error: model_catalog fetch failed: HTTP 500'`,
    );
    result.passed = true;
  } catch (err) {
    result.errors.push(String(err));
  } finally {
    if (harness.hooks.gateway_stop) {
      harness.hooks.gateway_stop({ reason: "test-cleanup" }, { port: 0 });
    }
    clearCache();
    setEnv({});
    await server.close();
    await rmTempAgentDir(agentDirPlugin);
    await rmTempAgentDir(agentDirControl);
  }
  return result;
}

// ── Main ─────────────────────────────────────────────────────────────────

async function main() {
  const results = [] as ScenarioResult[];
  results.push(await runScenarioA());
  results.push(await runScenarioB());
  results.push(await runScenarioC());

  let overallPass = true;
  for (const r of results) {
    console.log(`\n${r.passed ? "✓" : "✗"} ${r.name}`);
    for (const e of r.evidence) console.log(`   • ${e}`);
    for (const err of r.errors) console.log(`   ERROR: ${err}`);
    if (!r.passed) overallPass = false;
  }
  console.log(`\n${overallPass ? "ALL SCENARIOS PASSED" : "SOME SCENARIOS FAILED"}`);
  process.exit(overallPass ? 0 : 1);
}

main().catch((err) => {
  console.error("driver crashed:", err);
  process.exit(2);
});
