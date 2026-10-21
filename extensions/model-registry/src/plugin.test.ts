import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorCodes, errorShape } from "../../../src/gateway/protocol/index.js";
import type { OpenClawPluginApi } from "../../../src/plugins/types.js";
import plugin from "../index.js";
import { clearCache, getCachedProviders, refreshCache } from "./cache.js";

type CapturedHooks = Partial<Record<string, (event?: unknown, ctx?: unknown) => unknown>>;
type CapturedGatewayMethods = Partial<
  Record<string, Parameters<OpenClawPluginApi["registerGatewayMethod"]>[1]>
>;

interface Fixture {
  api: OpenClawPluginApi;
  hooks: CapturedHooks;
  gatewayMethods: CapturedGatewayMethods;
  logger: {
    debug: ReturnType<typeof vi.fn>;
    info: ReturnType<typeof vi.fn>;
    warn: ReturnType<typeof vi.fn>;
    error: ReturnType<typeof vi.fn>;
  };
}

function mkFixture(): Fixture {
  const hooks: CapturedHooks = {};
  const gatewayMethods: CapturedGatewayMethods = {};
  const logger = {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
  const api = {
    id: "model-registry",
    name: "Model Registry",
    on: vi.fn((name: string, handler: (event?: unknown, ctx?: unknown) => unknown) => {
      hooks[name] = handler;
    }),
    registerGatewayMethod: vi.fn(
      (method: string, handler: Parameters<OpenClawPluginApi["registerGatewayMethod"]>[1]) => {
        gatewayMethods[method] = handler;
      },
    ),
    // Every other api.* member is unused by this plugin; fill only what the
    // OpenClawPluginApi type contract forces us to declare (cast keeps the
    // fixture from having to stub the entire surface).
    logger,
  } as unknown as OpenClawPluginApi;

  return { api, hooks, gatewayMethods, logger };
}

function mockJsonResponse(body: unknown, init: { status?: number } = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json" },
  });
}

const acmeProvider = { baseUrl: "https://acme.example", apiKey: "ACME_KEY", models: [] };

describe("model-registry plugin", () => {
  let fx: Fixture;
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fx = mkFixture();
    fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    clearCache();
  });

  describe("register()", () => {
    it("subscribes to gateway_start, gateway_stop, and resolve_providers exactly once each", () => {
      plugin.register(fx.api);

      const onCalls = (fx.api.on as unknown as ReturnType<typeof vi.fn>).mock.calls;
      const hookNames = onCalls.map((call) => call[0]);
      expect(hookNames.filter((n) => n === "gateway_start")).toHaveLength(1);
      expect(hookNames.filter((n) => n === "gateway_stop")).toHaveLength(1);
      expect(hookNames.filter((n) => n === "resolve_providers")).toHaveLength(1);
    });

    it("registers the models.reload gateway method exactly once", () => {
      plugin.register(fx.api);

      const rgCalls = (fx.api.registerGatewayMethod as unknown as ReturnType<typeof vi.fn>).mock
        .calls;
      const methodNames = rgCalls.map((call) => call[0]);
      expect(methodNames.filter((n) => n === "models.reload")).toHaveLength(1);
    });
  });

  describe("gateway_start", () => {
    it("logs a warning and skips the fetch when CONTROL_PANEL_URL is unset", async () => {
      vi.stubEnv("CONTROL_PANEL_URL", "");
      vi.stubEnv("OPENCLAW_GATEWAY_TOKEN", "tok");

      plugin.register(fx.api);
      const start = fx.hooks["gateway_start"];
      expect(start).toBeTypeOf("function");

      await start!();

      expect(fx.logger.warn).toHaveBeenCalledWith(
        expect.stringMatching(/CONTROL_PANEL_URL not set.*disabled/i),
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it("performs the initial fetch and then a periodic refresh 5 minutes later", async () => {
      vi.stubEnv("CONTROL_PANEL_URL", "https://control.example");
      vi.stubEnv("OPENCLAW_GATEWAY_TOKEN", "tok");
      fetchSpy.mockResolvedValue(mockJsonResponse({ providers: { acme: acmeProvider } }));

      vi.useFakeTimers();
      plugin.register(fx.api);
      const start = fx.hooks["gateway_start"];
      await start!();
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      expect(fx.logger.info).toHaveBeenCalledWith(expect.stringMatching(/loaded 1 provider/));

      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    });

    it("logs a warning on initial fetch failure but does not throw", async () => {
      vi.stubEnv("CONTROL_PANEL_URL", "https://control.example");
      vi.stubEnv("OPENCLAW_GATEWAY_TOKEN", "tok");
      fetchSpy.mockResolvedValueOnce(mockJsonResponse({}, { status: 500 }));

      plugin.register(fx.api);
      const start = fx.hooks["gateway_start"];

      await expect(start!()).resolves.toBeUndefined();

      expect(fx.logger.warn).toHaveBeenCalledWith(
        expect.stringMatching(/initial cache load failed.*HTTP 500/i),
      );
    });
  });

  describe("gateway_stop", () => {
    it("clears the periodic timer and empties the cache", async () => {
      vi.stubEnv("CONTROL_PANEL_URL", "https://control.example");
      vi.stubEnv("OPENCLAW_GATEWAY_TOKEN", "tok");
      fetchSpy.mockResolvedValue(mockJsonResponse({ providers: { acme: acmeProvider } }));

      vi.useFakeTimers();
      plugin.register(fx.api);
      await fx.hooks["gateway_start"]!();
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      expect(getCachedProviders()).toEqual({ acme: acmeProvider });

      const stop = fx.hooks["gateway_stop"];
      expect(stop).toBeTypeOf("function");
      stop!();

      // Cache must be empty after stop.
      expect(getCachedProviders()).toEqual({});

      // Advancing the clock further must NOT fire another refresh — timer was cleared.
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });
  });

  describe("resolve_providers handler", () => {
    it("returns { providers: <cache contents> }", async () => {
      // Populate the cache via the real refreshCache path (fetch stub).
      fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: { acme: acmeProvider } }));
      await refreshCache("https://control.example", "tok");

      plugin.register(fx.api);
      const handler = fx.hooks["resolve_providers"];
      expect(handler).toBeTypeOf("function");
      const result = handler!({ agentDir: "/tmp", implicit: {}, explicit: {} }, {});

      expect(result).toEqual({ providers: { acme: acmeProvider } });
    });

    it("returns an empty providers map when the cache is empty", () => {
      plugin.register(fx.api);
      const handler = fx.hooks["resolve_providers"];
      const result = handler!({ agentDir: "/tmp", implicit: {}, explicit: {} }, {});

      expect(result).toEqual({ providers: {} });
    });
  });

  describe("models.reload RPC", () => {
    it("happy path: refreshes the cache and responds with { ok: true, count: <n> }", async () => {
      vi.stubEnv("CONTROL_PANEL_URL", "https://control.example");
      vi.stubEnv("OPENCLAW_GATEWAY_TOKEN", "tok");
      fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: { acme: acmeProvider } }));

      plugin.register(fx.api);
      const handler = fx.gatewayMethods["models.reload"];
      expect(handler).toBeDefined();
      const respond = vi.fn();

      await handler!({ params: undefined, respond } as unknown as Parameters<
        typeof handler
      >[0]);

      expect(respond).toHaveBeenCalledTimes(1);
      expect(respond.mock.calls[0][0]).toBe(true);
      // No providerCredentials in this fixture's catalog response, so
      // both maps come back empty — see refreshCredentialHealth's
      // early-return when getCachedCredentialRefs() is empty.
      expect(respond.mock.calls[0][1]).toEqual({
        ok: true, count: 1, credentials: {}, modelReachability: {},
      });
      expect(fx.logger.info).toHaveBeenCalledWith(
        expect.stringMatching(/cache refreshed.*1 provider/i),
      );
    });

    it("responds with UNAVAILABLE when CONTROL_PANEL_URL is unset", async () => {
      vi.stubEnv("CONTROL_PANEL_URL", "");
      vi.stubEnv("OPENCLAW_GATEWAY_TOKEN", "tok");

      plugin.register(fx.api);
      const handler = fx.gatewayMethods["models.reload"];
      const respond = vi.fn();

      await handler!({ params: undefined, respond } as unknown as Parameters<
        typeof handler
      >[0]);

      expect(respond).toHaveBeenCalledTimes(1);
      expect(respond.mock.calls[0][0]).toBe(false);
      expect(respond.mock.calls[0][1]).toBeUndefined();
      expect(respond.mock.calls[0][2]).toEqual(
        errorShape(ErrorCodes.UNAVAILABLE, "CONTROL_PANEL_URL not set"),
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it("responds with UNAVAILABLE and the HTTP status message on fetch failure", async () => {
      vi.stubEnv("CONTROL_PANEL_URL", "https://control.example");
      vi.stubEnv("OPENCLAW_GATEWAY_TOKEN", "tok");
      fetchSpy.mockResolvedValueOnce(mockJsonResponse({}, { status: 500 }));

      plugin.register(fx.api);
      const handler = fx.gatewayMethods["models.reload"];
      const respond = vi.fn();

      await handler!({ params: undefined, respond } as unknown as Parameters<
        typeof handler
      >[0]);

      expect(respond).toHaveBeenCalledTimes(1);
      expect(respond.mock.calls[0][0]).toBe(false);
      expect(respond.mock.calls[0][1]).toBeUndefined();
      const err = respond.mock.calls[0][2];
      expect(err.code).toBe(ErrorCodes.UNAVAILABLE);
      expect(err.message).toMatch(/HTTP 500/);
    });
  });
});
