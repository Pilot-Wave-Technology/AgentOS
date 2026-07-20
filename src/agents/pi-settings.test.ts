import { describe, expect, it, vi } from "vitest";
import {
  applyPiCompactionSettingsFromConfig,
  DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR,
  resolveCompactionEffectiveContextWindow,
  resolveCompactionReserveTokensFloor,
} from "./pi-settings.js";

vi.mock("./context.js", () => ({
  lookupContextTokens: (modelId?: string) => {
    if (!modelId) return undefined;
    if (modelId.includes("gemini-2.5-flash")) return 1_000_000;
    if (modelId.includes("claude-sonnet-4-5")) return 200_000;
    return undefined;
  },
}));

describe("applyPiCompactionSettingsFromConfig", () => {
  it("bumps reserveTokens when below floor", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 16_384,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({ settingsManager });

    expect(result.didOverride).toBe(true);
    expect(result.compaction.reserveTokens).toBe(DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR);
    expect(settingsManager.applyOverrides).toHaveBeenCalledWith({
      compaction: { reserveTokens: DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR },
    });
  });

  it("does not override when already above floor and not in safeguard mode", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 32_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: { agents: { defaults: { compaction: { mode: "default" } } } },
    });

    expect(result.didOverride).toBe(false);
    expect(result.compaction.reserveTokens).toBe(32_000);
    expect(settingsManager.applyOverrides).not.toHaveBeenCalled();
  });

  it("applies explicit reserveTokens but still enforces floor", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 10_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: {
        agents: {
          defaults: {
            compaction: { reserveTokens: 12_000, reserveTokensFloor: 20_000 },
          },
        },
      },
    });

    expect(result.compaction.reserveTokens).toBe(20_000);
    expect(settingsManager.applyOverrides).toHaveBeenCalledWith({
      compaction: { reserveTokens: 20_000 },
    });
  });

  it("applies keepRecentTokens when explicitly configured", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 20_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: {
        agents: {
          defaults: {
            compaction: {
              keepRecentTokens: 15_000,
            },
          },
        },
      },
    });

    expect(result.compaction.keepRecentTokens).toBe(15_000);
    expect(settingsManager.applyOverrides).toHaveBeenCalledWith({
      compaction: { keepRecentTokens: 15_000 },
    });
  });

  it("preserves current keepRecentTokens when safeguard mode leaves it unset", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 25_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: { agents: { defaults: { compaction: { mode: "safeguard" } } } },
    });

    expect(result.compaction.keepRecentTokens).toBe(20_000);
    expect(settingsManager.applyOverrides).not.toHaveBeenCalled();
  });

  it("treats keepRecentTokens=0 as invalid and keeps the current setting", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 25_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: { agents: { defaults: { compaction: { mode: "safeguard", keepRecentTokens: 0 } } } },
    });

    expect(result.compaction.keepRecentTokens).toBe(20_000);
    expect(settingsManager.applyOverrides).not.toHaveBeenCalled();
  });

  it("keepRecentTokensPct resolves against the model's native context window", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 25_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: { agents: { defaults: { compaction: { keepRecentTokensPct: 0.3 } } } },
      modelId: "openrouter/anthropic/claude-sonnet-4-5",
    });

    // 200_000 * 0.3 = 60_000
    expect(result.compaction.keepRecentTokens).toBe(60_000);
    expect(settingsManager.applyOverrides).toHaveBeenCalledWith({
      compaction: { keepRecentTokens: 60_000 },
    });
  });

  it("keepRecentTokensPct honors agents.defaults.contextTokens cap (dev profile)", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 25_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: {
        agents: {
          defaults: {
            contextTokens: 50_000,
            compaction: { keepRecentTokensPct: 0.3 },
          },
        },
      },
      modelId: "google/gemini-2.5-flash",
    });

    // Effective window = min(50_000 cap, 1_000_000 native) = 50_000; 50_000 * 0.3 = 15_000
    expect(result.compaction.keepRecentTokens).toBe(15_000);
    expect(settingsManager.applyOverrides).toHaveBeenCalledWith({
      compaction: { keepRecentTokens: 15_000 },
    });
  });

  it("keepRecentTokensPct overrides the absolute keepRecentTokens when both are set", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 25_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: {
        agents: {
          defaults: {
            compaction: { keepRecentTokens: 5_000, keepRecentTokensPct: 0.3 },
          },
        },
      },
      modelId: "openrouter/anthropic/claude-sonnet-4-5",
    });

    expect(result.compaction.keepRecentTokens).toBe(60_000);
  });

  it("keepRecentTokensPct falls back to the absolute when contextWindow cannot be resolved", () => {
    const settingsManager = {
      getCompactionReserveTokens: () => 25_000,
      getCompactionKeepRecentTokens: () => 20_000,
      applyOverrides: vi.fn(),
    };

    const result = applyPiCompactionSettingsFromConfig({
      settingsManager,
      cfg: {
        agents: {
          defaults: {
            compaction: { keepRecentTokens: 8_000, keepRecentTokensPct: 0.3 },
          },
        },
      },
      modelId: "unknown/some-mystery-model",
    });

    // No native lookup, no cap → falls back to absolute keepRecentTokens
    expect(result.compaction.keepRecentTokens).toBe(8_000);
  });
});

describe("resolveCompactionEffectiveContextWindow", () => {
  it("returns the model's native window when no cap is set", () => {
    expect(
      resolveCompactionEffectiveContextWindow({
        modelId: "openrouter/anthropic/claude-sonnet-4-5",
      }),
    ).toBe(200_000);
  });

  it("applies the agents.defaults.contextTokens cap when lower than native", () => {
    expect(
      resolveCompactionEffectiveContextWindow({
        cfg: { agents: { defaults: { contextTokens: 50_000 } } },
        modelId: "google/gemini-2.5-flash",
      }),
    ).toBe(50_000);
  });

  it("returns the cap when the model is unknown", () => {
    expect(
      resolveCompactionEffectiveContextWindow({
        cfg: { agents: { defaults: { contextTokens: 50_000 } } },
        modelId: "unknown/some-model",
      }),
    ).toBe(50_000);
  });

  it("returns undefined when nothing can be resolved", () => {
    expect(resolveCompactionEffectiveContextWindow({})).toBeUndefined();
  });
});

describe("resolveCompactionReserveTokensFloor", () => {
  it("returns the default when config is missing", () => {
    expect(resolveCompactionReserveTokensFloor()).toBe(DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR);
  });

  it("accepts configured floors, including zero", () => {
    expect(
      resolveCompactionReserveTokensFloor({
        agents: { defaults: { compaction: { reserveTokensFloor: 24_000 } } },
      }),
    ).toBe(24_000);
    expect(
      resolveCompactionReserveTokensFloor({
        agents: { defaults: { compaction: { reserveTokensFloor: 0 } } },
      }),
    ).toBe(0);
  });
});
