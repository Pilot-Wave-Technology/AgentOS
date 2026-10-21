import { describe, expect, it } from "vitest";
import type { OpenClawConfig } from "../config/config.js";
import {
  CONTEXT_WINDOW_HARD_MIN_TOKENS,
  CONTEXT_WINDOW_WARN_BELOW_TOKENS,
  evaluateContextWindowGuard,
  getAgentContextTokens,
  resolveContextWindowInfo,
} from "./context-window-guard.js";

describe("context-window-guard", () => {
  it("blocks below 16k (model metadata)", () => {
    const info = resolveContextWindowInfo({
      cfg: undefined,
      provider: "openrouter",
      modelId: "tiny",
      modelContextWindow: 8000,
      defaultTokens: 200_000,
    });
    const guard = evaluateContextWindowGuard({ info });
    expect(guard.source).toBe("model");
    expect(guard.tokens).toBe(8000);
    expect(guard.shouldWarn).toBe(true);
    expect(guard.shouldBlock).toBe(true);
  });

  it("warns below 32k but does not block at 16k+", () => {
    const info = resolveContextWindowInfo({
      cfg: undefined,
      provider: "openai",
      modelId: "small",
      modelContextWindow: 24_000,
      defaultTokens: 200_000,
    });
    const guard = evaluateContextWindowGuard({ info });
    expect(guard.tokens).toBe(24_000);
    expect(guard.shouldWarn).toBe(true);
    expect(guard.shouldBlock).toBe(false);
  });

  it("does not warn at 32k+ (model metadata)", () => {
    const info = resolveContextWindowInfo({
      cfg: undefined,
      provider: "openai",
      modelId: "ok",
      modelContextWindow: 64_000,
      defaultTokens: 200_000,
    });
    const guard = evaluateContextWindowGuard({ info });
    expect(guard.shouldWarn).toBe(false);
    expect(guard.shouldBlock).toBe(false);
  });

  it("uses models.providers.*.models[].contextWindow when present", () => {
    const cfg = {
      models: {
        providers: {
          openrouter: {
            baseUrl: "http://localhost",
            apiKey: "x",
            models: [
              {
                id: "tiny",
                name: "tiny",
                reasoning: false,
                input: ["text"],
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
                contextWindow: 12_000,
                maxTokens: 256,
              },
            ],
          },
        },
      },
    } satisfies OpenClawConfig;

    const info = resolveContextWindowInfo({
      cfg,
      provider: "openrouter",
      modelId: "tiny",
      modelContextWindow: 64_000,
      defaultTokens: 200_000,
    });
    const guard = evaluateContextWindowGuard({ info });
    expect(info.source).toBe("modelsConfig");
    expect(guard.shouldBlock).toBe(true);
  });

  it("caps with agents.defaults.contextTokens", () => {
    const cfg = {
      agents: { defaults: { contextTokens: 20_000 } },
    } satisfies OpenClawConfig;
    const info = resolveContextWindowInfo({
      cfg,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 200_000,
      defaultTokens: 200_000,
    });
    const guard = evaluateContextWindowGuard({ info });
    expect(info.source).toBe("agentContextTokens");
    expect(guard.shouldWarn).toBe(true);
    expect(guard.shouldBlock).toBe(false);
  });

  it("does not override when cap exceeds base window", () => {
    const cfg = {
      agents: { defaults: { contextTokens: 128_000 } },
    } satisfies OpenClawConfig;
    const info = resolveContextWindowInfo({
      cfg,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 64_000,
      defaultTokens: 200_000,
    });
    expect(info.source).toBe("model");
    expect(info.tokens).toBe(64_000);
  });

  it("uses default when nothing else is available", () => {
    const info = resolveContextWindowInfo({
      cfg: undefined,
      provider: "anthropic",
      modelId: "unknown",
      modelContextWindow: undefined,
      defaultTokens: 200_000,
    });
    const guard = evaluateContextWindowGuard({ info });
    expect(info.source).toBe("default");
    expect(guard.shouldWarn).toBe(false);
    expect(guard.shouldBlock).toBe(false);
  });

  it("allows overriding thresholds", () => {
    const info = { tokens: 10_000, source: "model" as const };
    const guard = evaluateContextWindowGuard({
      info,
      warnBelowTokens: 12_000,
      hardMinTokens: 9_000,
    });
    expect(guard.shouldWarn).toBe(true);
    expect(guard.shouldBlock).toBe(false);
  });

  it("exports thresholds as expected", () => {
    expect(CONTEXT_WINDOW_HARD_MIN_TOKENS).toBe(16_000);
    expect(CONTEXT_WINDOW_WARN_BELOW_TOKENS).toBe(32_000);
  });
});

describe("context-window-guard per-agent contextTokens cap (cb11f6b9)", () => {
  it("applies agentContextTokens when defaults.contextTokens is unset", () => {
    const info = resolveContextWindowInfo({
      cfg: undefined,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 200_000,
      defaultTokens: 200_000,
      agentContextTokens: 32_000,
    });
    expect(info.source).toBe("agentContextTokens");
    expect(info.tokens).toBe(32_000);
  });

  it("per-agent cap wins over a larger global default (50k beats 100k)", () => {
    const cfg = {
      agents: { defaults: { contextTokens: 100_000 } },
    } satisfies OpenClawConfig;
    const info = resolveContextWindowInfo({
      cfg,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 200_000,
      defaultTokens: 200_000,
      agentContextTokens: 50_000,
    });
    expect(info.source).toBe("agentContextTokens");
    expect(info.tokens).toBe(50_000);
  });

  it("per-agent cap wins even when it is LARGER than the global default (100k beats 50k)", () => {
    // Regression: per-agent must not be silently clamped down by a smaller
    // global default. The defaults value is completely ignored once the
    // per-agent cap is set.
    const cfg = {
      agents: { defaults: { contextTokens: 50_000 } },
    } satisfies OpenClawConfig;
    const info = resolveContextWindowInfo({
      cfg,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 200_000,
      defaultTokens: 200_000,
      agentContextTokens: 100_000,
    });
    expect(info.source).toBe("agentContextTokens");
    expect(info.tokens).toBe(100_000);
  });

  it("agentContextTokens above the model window falls through to the model (cap only clamps down)", () => {
    const info = resolveContextWindowInfo({
      cfg: undefined,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 200_000,
      defaultTokens: 200_000,
      agentContextTokens: 500_000,
    });
    expect(info.source).toBe("model");
    expect(info.tokens).toBe(200_000);
  });

  it("agentContextTokens unset falls back to defaults.contextTokens (backward-compat)", () => {
    const cfg = {
      agents: { defaults: { contextTokens: 32_000 } },
    } satisfies OpenClawConfig;
    const info = resolveContextWindowInfo({
      cfg,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 200_000,
      defaultTokens: 200_000,
    });
    expect(info.source).toBe("agentContextTokens");
    expect(info.tokens).toBe(32_000);
  });

  it("ignores non-positive or non-numeric agentContextTokens", () => {
    // Same normalizePositiveInt guard as the global cap: 0 and negatives
    // are treated as unset, so we fall through to the base info.
    const info = resolveContextWindowInfo({
      cfg: undefined,
      provider: "anthropic",
      modelId: "whatever",
      modelContextWindow: 200_000,
      defaultTokens: 200_000,
      agentContextTokens: 0,
    });
    expect(info.source).toBe("model");
    expect(info.tokens).toBe(200_000);
  });
});

describe("getAgentContextTokens (cb11f6b9)", () => {
  it("returns the per-agent contextTokens when the entry exists", () => {
    const cfg = {
      agents: { list: [{ id: "shca", contextTokens: 32_000 }] },
    } satisfies OpenClawConfig;
    expect(getAgentContextTokens(cfg, "shca")).toBe(32_000);
  });

  it("matches agent ids case-insensitively", () => {
    const cfg = {
      agents: { list: [{ id: "SHCA", contextTokens: 32_000 }] },
    } satisfies OpenClawConfig;
    expect(getAgentContextTokens(cfg, "shca")).toBe(32_000);
    expect(getAgentContextTokens(cfg, "SHCA")).toBe(32_000);
    expect(getAgentContextTokens(cfg, "Shca")).toBe(32_000);
  });

  it("returns undefined when the entry has no contextTokens", () => {
    const cfg = {
      agents: { list: [{ id: "shca" }] },
    } satisfies OpenClawConfig;
    expect(getAgentContextTokens(cfg, "shca")).toBeUndefined();
  });

  it("returns undefined when no matching entry is found", () => {
    const cfg = {
      agents: { list: [{ id: "shca", contextTokens: 32_000 }] },
    } satisfies OpenClawConfig;
    expect(getAgentContextTokens(cfg, "unknown")).toBeUndefined();
  });

  it("returns undefined when cfg or agentId is missing", () => {
    expect(getAgentContextTokens(undefined, "shca")).toBeUndefined();
    expect(getAgentContextTokens({} as OpenClawConfig, "shca")).toBeUndefined();
    expect(getAgentContextTokens({ agents: {} } as OpenClawConfig, "shca")).toBeUndefined();
    expect(getAgentContextTokens({ agents: { list: [] } } as OpenClawConfig, undefined))
      .toBeUndefined();
  });

  it("returns undefined for a non-positive stored value", () => {
    const cfg = {
      agents: { list: [{ id: "shca", contextTokens: 0 as unknown as number }] },
    } satisfies OpenClawConfig;
    expect(getAgentContextTokens(cfg, "shca")).toBeUndefined();
  });
});
