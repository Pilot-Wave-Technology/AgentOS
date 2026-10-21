import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProviderConfig } from "../agents/models-config.providers.js";
import { RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS, createHookRunner } from "./hooks.js";
import { createEmptyPluginRegistry, type PluginRegistry } from "./registry.js";
import type {
  PluginHookRegistration,
  PluginHookResolveProvidersEvent,
  PluginHookResolveProvidersResult,
} from "./types.js";

type ResolveHandler = (
  event: PluginHookResolveProvidersEvent,
) => Promise<PluginHookResolveProvidersResult | void> | PluginHookResolveProvidersResult | void;

function addResolveHook(
  registry: PluginRegistry,
  pluginId: string,
  handler: ResolveHandler,
  priority?: number,
) {
  registry.typedHooks.push({
    pluginId,
    hookName: "resolve_providers",
    handler,
    priority,
    source: "test",
  } as PluginHookRegistration);
}

function makeEvent(): PluginHookResolveProvidersEvent {
  return {
    agentDir: "/tmp/openclaw-test-agent",
    implicit: {},
    explicit: {},
  };
}

function makeLogger() {
  return {
    debug: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
}

const dummyCtx = {};

const acmeProvider: ProviderConfig = {
  baseUrl: "https://acme.example",
  apiKey: "ACME_KEY",
  models: [],
};

const betaProvider: ProviderConfig = {
  baseUrl: "https://beta.example",
  apiKey: "BETA_KEY",
  models: [],
};

const overrideOllama: ProviderConfig = {
  baseUrl: "https://ollama.override.example",
  models: [],
};

describe("runResolveProviders", () => {
  let registry: PluginRegistry;

  beforeEach(() => {
    registry = createEmptyPluginRegistry();
  });

  it("returns empty accumulator when no handlers are registered", async () => {
    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });
    expect(outcome).toEqual({ providers: {}, collisions: [] });
  });

  it("collects a single handler's contribution", async () => {
    addResolveHook(registry, "acme-plugin", () => ({ providers: { acme: acmeProvider } }));

    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers).toEqual({ acme: acmeProvider });
    expect(outcome.collisions).toEqual([]);
  });

  it("merges disjoint keys from two handlers", async () => {
    addResolveHook(registry, "acme-plugin", () => ({ providers: { acme: acmeProvider } }));
    addResolveHook(registry, "beta-plugin", () => ({ providers: { beta: betaProvider } }));

    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers).toEqual({ acme: acmeProvider, beta: betaProvider });
    expect(outcome.collisions).toEqual([]);
  });

  it("keeps first-set value on collision and records the losing plugin", async () => {
    // Both handlers registered at the same priority; registration order decides.
    addResolveHook(registry, "acme-plugin", () => ({ providers: { ollama: overrideOllama } }));
    addResolveHook(registry, "beta-plugin", () => ({
      providers: { ollama: { baseUrl: "https://beta.override.example", models: [] } },
    }));

    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers.ollama).toEqual(overrideOllama);
    expect(outcome.collisions).toEqual([{ pluginId: "beta-plugin", key: "ollama" }]);
  });

  it("higher-priority handler wins on collision", async () => {
    // Priority 1 (low) registers first, priority 10 (high) registers second.
    // getHooksForName sorts by priority DESC before dispatch.
    addResolveHook(
      registry,
      "low-priority",
      () => ({ providers: { acme: { baseUrl: "https://low.example", models: [] } } }),
      1,
    );
    addResolveHook(registry, "high-priority", () => ({ providers: { acme: acmeProvider } }), 10);

    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers.acme).toEqual(acmeProvider);
    // The low-priority handler runs *after* the high-priority one, so its
    // colliding key surfaces as the loser in the collisions list.
    expect(outcome.collisions).toEqual([{ pluginId: "low-priority", key: "acme" }]);
  });

  it("catches a thrown handler and continues with the remaining handlers", async () => {
    const logger = makeLogger();
    addResolveHook(registry, "throwing-plugin", () => {
      throw new Error("boom");
    });
    addResolveHook(registry, "acme-plugin", () => ({ providers: { acme: acmeProvider } }));

    const runner = createHookRunner(registry, { logger });
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers).toEqual({ acme: acmeProvider });
    expect(outcome.collisions).toEqual([]);
    expect(logger.error).toHaveBeenCalledTimes(1);
    const errorMsg = logger.error.mock.calls[0]?.[0];
    expect(errorMsg).toContain("resolve_providers");
    expect(errorMsg).toContain("throwing-plugin");
    expect(errorMsg).toContain("boom");
  });

  it("treats undefined / void returns as no contribution", async () => {
    addResolveHook(registry, "void-plugin", () => undefined);
    addResolveHook(registry, "no-providers-plugin", () => ({}));
    addResolveHook(registry, "acme-plugin", () => ({ providers: { acme: acmeProvider } }));

    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers).toEqual({ acme: acmeProvider });
    expect(outcome.collisions).toEqual([]);
  });

  it("awaits a handler that returns a Promise", async () => {
    addResolveHook(registry, "async-plugin", async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { providers: { acme: acmeProvider } };
    });

    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers).toEqual({ acme: acmeProvider });
    expect(outcome.collisions).toEqual([]);
  });

  it(
    "skips a handler that exceeds the soft timeout budget and continues",
    async () => {
      const logger = makeLogger();
      addResolveHook(registry, "slow-plugin", async () => {
        await new Promise((resolve) =>
          setTimeout(resolve, RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS + 200),
        );
        return { providers: { slow: acmeProvider } };
      });
      addResolveHook(registry, "acme-plugin", () => ({ providers: { acme: acmeProvider } }));

      const runner = createHookRunner(registry, { logger });
      const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

      expect(outcome.providers).toEqual({ acme: acmeProvider });
      expect(outcome.collisions).toEqual([]);
      expect(logger.warn).toHaveBeenCalledTimes(1);
      const warnMsg = logger.warn.mock.calls[0]?.[0];
      expect(warnMsg).toContain("resolve_providers");
      expect(warnMsg).toContain("slow-plugin");
      expect(warnMsg).toContain(`${RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS}ms budget`);

      // The same signal must also reach `plugins.diagnostics` so the plugin
      // status view surfaces the timeout to operators, matching what
      // documentation/tools/plugin.md promises.
      expect(registry.diagnostics).toHaveLength(1);
      const diag = registry.diagnostics[0];
      expect(diag?.level).toBe("warn");
      expect(diag?.pluginId).toBe("slow-plugin");
      expect(diag?.source).toBe("slow-plugin");
      expect(diag?.message).toContain("slow-plugin");
      expect(diag?.message).toContain(`${RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS}ms budget`);
    },
    // Test wall-clock ceiling: timeout budget + slack for handler drain.
    RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS + 5_000,
  );

  it("skips empty / whitespace-only keys returned by a handler", async () => {
    addResolveHook(registry, "whitespace-plugin", () => ({
      providers: {
        "  ": acmeProvider,
        "": betaProvider,
        acme: acmeProvider,
      },
    }));

    const runner = createHookRunner(registry);
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(Object.keys(outcome.providers)).toEqual(["acme"]);
    expect(outcome.collisions).toEqual([]);
  });

  it(
    "drains a late rejection after the timeout so it does not become an unhandled rejection",
    async () => {
      // A plugin whose handler rejects AFTER the soft timeout has already
      // fired must not leak an unhandled promise rejection — under Node's
      // default `--unhandled-rejections=throw` that would terminate the
      // gateway. The runner's timeout branch attaches a no-op catch to
      // handlerPromise; this test guards that invariant.
      const unhandled: unknown[] = [];
      const listener = (reason: unknown) => {
        unhandled.push(reason);
      };
      process.on("unhandledRejection", listener);
      try {
        addResolveHook(registry, "late-rejecting-plugin", async () => {
          await new Promise((_resolve, reject) => {
            setTimeout(
              () => reject(new Error("late boom")),
              RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS + 100,
            );
          });
          return { providers: {} };
        });
        addResolveHook(registry, "acme-plugin", () => ({
          providers: { acme: acmeProvider },
        }));

        const runner = createHookRunner(registry);
        const outcome = await runner.runResolveProviders({
          event: makeEvent(),
          ctx: dummyCtx,
        });

        // Give the late rejection ample time to fire. If T2's no-op catch
        // were missing, Node would emit "unhandledRejection" during this
        // wait and `unhandled` would be non-empty.
        await new Promise((resolve) =>
          setTimeout(resolve, RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS + 300),
        );

        expect(outcome.providers).toEqual({ acme: acmeProvider });
        expect(unhandled).toEqual([]);
      } finally {
        process.off("unhandledRejection", listener);
      }
    },
    // Test wall-clock ceiling: timeout budget + rejection delay + drain
    // window + generous slack.
    RESOLVE_PROVIDERS_HANDLER_TIMEOUT_MS + 5_000,
  );

  it("still logs an error via handleHookError when a handler rejects fast (before the timeout window closes)", async () => {
    // Fast rejections (before the race resolves via timeout) must still
    // surface through the outer try/catch and reach handleHookError so
    // operators see the failure. T2's fix is deliberately narrow — the
    // no-op catch is scoped to the timeout branch only, so this pre-cycle-02
    // behaviour must stay intact.
    const logger = makeLogger();
    addResolveHook(registry, "fast-rejecting-plugin", () => {
      return Promise.reject(new Error("fast boom"));
    });
    addResolveHook(registry, "acme-plugin", () => ({ providers: { acme: acmeProvider } }));

    const runner = createHookRunner(registry, { logger });
    const outcome = await runner.runResolveProviders({ event: makeEvent(), ctx: dummyCtx });

    expect(outcome.providers).toEqual({ acme: acmeProvider });
    expect(outcome.collisions).toEqual([]);
    expect(logger.error).toHaveBeenCalledTimes(1);
    const errorMsg = logger.error.mock.calls[0]?.[0];
    expect(errorMsg).toContain("resolve_providers");
    expect(errorMsg).toContain("fast-rejecting-plugin");
    expect(errorMsg).toContain("fast boom");
    // No timeout diagnostic should be recorded — this rejection never
    // reached the timeout branch.
    expect(registry.diagnostics).toEqual([]);
  });
});
