import { beforeEach, describe, expect, it, vi } from "vitest";

function mockContextModuleDeps(loadConfigImpl: () => unknown) {
  vi.doMock("../config/config.js", () => ({
    loadConfig: loadConfigImpl,
  }));
  vi.doMock("./models-config.js", () => ({
    ensureOpenClawModelsJson: vi.fn(async () => {}),
  }));
  vi.doMock("./agent-paths.js", () => ({
    resolveOpenClawAgentDir: () => "/tmp/openclaw-agent",
  }));
  vi.doMock("./pi-model-discovery.js", () => ({
    discoverAuthStorage: vi.fn(() => ({})),
    discoverModels: vi.fn(() => ({
      getAll: () => [],
    })),
  }));
}

describe("lookupContextTokens", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("returns configured model context window on first lookup", async () => {
    mockContextModuleDeps(() => ({
      models: {
        providers: {
          openrouter: {
            models: [{ id: "openrouter/claude-sonnet", contextWindow: 321_000 }],
          },
        },
      },
    }));

    const { lookupContextTokens } = await import("./context.js");
    expect(lookupContextTokens("openrouter/claude-sonnet")).toBe(321_000);
  });

  it("does not skip eager warmup when --profile is followed by -- terminator", async () => {
    const loadConfigMock = vi.fn(() => ({ models: {} }));
    mockContextModuleDeps(loadConfigMock);

    const argvSnapshot = process.argv;
    process.argv = ["node", "openclaw", "--profile", "--", "config", "validate"];
    try {
      await import("./context.js");
      expect(loadConfigMock).toHaveBeenCalledTimes(1);
    } finally {
      process.argv = argvSnapshot;
    }
  });

  it("retries config loading after backoff when an initial load fails", async () => {
    vi.useFakeTimers();
    const loadConfigMock = vi
      .fn()
      .mockImplementationOnce(() => {
        throw new Error("transient");
      })
      .mockImplementation(() => ({
        models: {
          providers: {
            openrouter: {
              models: [{ id: "openrouter/claude-sonnet", contextWindow: 654_321 }],
            },
          },
        },
      }));

    mockContextModuleDeps(loadConfigMock);

    const argvSnapshot = process.argv;
    process.argv = ["node", "openclaw", "config", "validate"];
    try {
      const { lookupContextTokens } = await import("./context.js");
      expect(lookupContextTokens("openrouter/claude-sonnet")).toBeUndefined();
      expect(loadConfigMock).toHaveBeenCalledTimes(1);
      expect(lookupContextTokens("openrouter/claude-sonnet")).toBeUndefined();
      expect(loadConfigMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(lookupContextTokens("openrouter/claude-sonnet")).toBe(654_321);
      expect(loadConfigMock).toHaveBeenCalledTimes(2);
    } finally {
      process.argv = argvSnapshot;
      vi.useRealTimers();
    }
  });

  it("picks up a model that arrived after the first load, on a later miss", async () => {
    vi.useFakeTimers();
    // The registry's synthetic variants land in models.json after boot.
    let models: { id: string; contextWindow: number }[] = [];
    mockContextModuleDeps(() => ({ models: {} }));
    vi.doMock("./pi-model-discovery.js", () => ({
      discoverAuthStorage: vi.fn(() => ({})),
      discoverModels: vi.fn(() => ({ getAll: () => models })),
    }));
    try {
      const { lookupContextTokens } = await import("./context.js");
      expect(lookupContextTokens("gemini-3.8-flash-500k")).toBeUndefined();
      await vi.advanceTimersByTimeAsync(0);

      models = [{ id: "gemini-3.8-flash-500k", contextWindow: 500_000 }];
      // Inside the rate limit: still the old cache.
      expect(lookupContextTokens("gemini-3.8-flash-500k")).toBeUndefined();

      await vi.advanceTimersByTimeAsync(30_000);
      lookupContextTokens("gemini-3.8-flash-500k"); // the miss that reloads
      await vi.advanceTimersByTimeAsync(0);
      expect(lookupContextTokens("gemini-3.8-flash-500k")).toBe(500_000);
    } finally {
      vi.useRealTimers();
    }
  });
});
