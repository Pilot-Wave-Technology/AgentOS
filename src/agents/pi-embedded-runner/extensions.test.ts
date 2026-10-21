import type { Api, Model } from "@mariozechner/pi-ai";
import type { SessionManager } from "@mariozechner/pi-coding-agent";
import { describe, expect, it } from "vitest";
import type { OpenClawConfig } from "../../config/config.js";
import { getCompactionSafeguardRuntime } from "../pi-extensions/compaction-safeguard-runtime.js";
import compactionSafeguardExtension from "../pi-extensions/compaction-safeguard.js";
import { getContextPruningRuntime } from "../pi-extensions/context-pruning/runtime.js";
import contextPruningExtension from "../pi-extensions/context-pruning.js";
import { buildEmbeddedExtensionFactories } from "./extensions.js";

describe("buildEmbeddedExtensionFactories", () => {
  it("does not opt safeguard mode into quality-guard retries", () => {
    const sessionManager = {} as SessionManager;
    const model = {
      id: "claude-sonnet-4-20250514",
      contextWindow: 200_000,
    } as Model<Api>;
    const cfg = {
      agents: {
        defaults: {
          compaction: {
            mode: "safeguard",
          },
        },
      },
    } as OpenClawConfig;

    const factories = buildEmbeddedExtensionFactories({
      cfg,
      sessionManager,
      provider: "anthropic",
      modelId: "claude-sonnet-4-20250514",
      model,
    });

    expect(factories).toContain(compactionSafeguardExtension);
    expect(getCompactionSafeguardRuntime(sessionManager)).toMatchObject({
      qualityGuardEnabled: false,
    });
  });

  it("wires explicit safeguard quality-guard runtime flags", () => {
    const sessionManager = {} as SessionManager;
    const model = {
      id: "claude-sonnet-4-20250514",
      contextWindow: 200_000,
    } as Model<Api>;
    const cfg = {
      agents: {
        defaults: {
          compaction: {
            mode: "safeguard",
            qualityGuard: {
              enabled: true,
              maxRetries: 2,
            },
          },
        },
      },
    } as OpenClawConfig;

    const factories = buildEmbeddedExtensionFactories({
      cfg,
      sessionManager,
      provider: "anthropic",
      modelId: "claude-sonnet-4-20250514",
      model,
    });

    expect(factories).toContain(compactionSafeguardExtension);
    expect(getCompactionSafeguardRuntime(sessionManager)).toMatchObject({
      qualityGuardEnabled: true,
      qualityGuardMaxRetries: 2,
    });
  });
});

describe("buildEmbeddedExtensionFactories — per-agent contextTokens (acd626a5)", () => {
  it("safeguard runtime uses per-agent contextTokens instead of the model's 1M window", () => {
    const sessionManager = {} as SessionManager;
    const model = {
      id: "gemini-2.5-flash",
      contextWindow: 1_000_000,
    } as Model<Api>;
    const cfg = {
      agents: {
        defaults: {
          compaction: {
            mode: "safeguard",
            keepRecentTokensPct: 0.5,
          },
        },
        list: [{ id: "x", contextTokens: 32_000 }],
      },
    } as OpenClawConfig;

    const factories = buildEmbeddedExtensionFactories({
      cfg,
      sessionManager,
      provider: "gemini",
      modelId: "gemini-2.5-flash",
      model,
      agentId: "x",
    });

    expect(factories).toContain(compactionSafeguardExtension);
    const runtime = getCompactionSafeguardRuntime(sessionManager);
    expect(runtime?.contextWindowTokens).toBe(32_000);
    // keepRecentTokensBudget is 0.5 * contextWindowTokens = 16_000 (not 500_000)
    expect(runtime?.keepRecentTokensBudget).toBe(16_000);
  });

  it("safeguard runtime falls back to the model window when the agent has no contextTokens", () => {
    const sessionManager = {} as SessionManager;
    const model = {
      id: "claude-sonnet-4-20250514",
      contextWindow: 200_000,
    } as Model<Api>;
    const cfg = {
      agents: {
        defaults: {
          compaction: {
            mode: "safeguard",
          },
        },
        list: [{ id: "x" }],
      },
    } as OpenClawConfig;

    const factories = buildEmbeddedExtensionFactories({
      cfg,
      sessionManager,
      provider: "anthropic",
      modelId: "claude-sonnet-4-20250514",
      model,
      agentId: "x",
    });

    expect(factories).toContain(compactionSafeguardExtension);
    expect(getCompactionSafeguardRuntime(sessionManager)?.contextWindowTokens).toBe(200_000);
  });

  it("context-pruning runtime uses per-agent contextTokens on a 1M model", () => {
    const sessionManager = {} as SessionManager;
    const model = {
      id: "claude-sonnet-4-5-1m",
      contextWindow: 1_000_000,
    } as Model<Api>;
    const cfg = {
      agents: {
        defaults: {
          contextPruning: {
            mode: "cache-ttl",
          },
        },
        list: [{ id: "y", contextTokens: 50_000 }],
      },
    } as OpenClawConfig;

    const factories = buildEmbeddedExtensionFactories({
      cfg,
      sessionManager,
      provider: "anthropic",
      modelId: "claude-sonnet-4-5-1m",
      model,
      agentId: "y",
    });

    expect(factories).toContain(contextPruningExtension);
    expect(getContextPruningRuntime(sessionManager)?.contextWindowTokens).toBe(50_000);
  });

  it("per-agent contextTokens above the model window returns the model window (clamp-only)", () => {
    const sessionManager = {} as SessionManager;
    const model = {
      id: "claude-sonnet-4-20250514",
      contextWindow: 200_000,
    } as Model<Api>;
    const cfg = {
      agents: {
        defaults: {
          compaction: {
            mode: "safeguard",
          },
        },
        list: [{ id: "big", contextTokens: 500_000 }],
      },
    } as OpenClawConfig;

    const factories = buildEmbeddedExtensionFactories({
      cfg,
      sessionManager,
      provider: "anthropic",
      modelId: "claude-sonnet-4-20250514",
      model,
      agentId: "big",
    });

    expect(factories).toContain(compactionSafeguardExtension);
    expect(getCompactionSafeguardRuntime(sessionManager)?.contextWindowTokens).toBe(200_000);
  });

  it("baseline behaviour is unchanged when agentId is undefined", () => {
    const sessionManager = {} as SessionManager;
    const model = {
      id: "claude-sonnet-4-20250514",
      contextWindow: 200_000,
    } as Model<Api>;
    const cfg = {
      agents: {
        defaults: {
          compaction: {
            mode: "safeguard",
          },
        },
        // Even if the list has an agent with contextTokens, no agentId means no lookup.
        list: [{ id: "x", contextTokens: 32_000 }],
      },
    } as OpenClawConfig;

    const factories = buildEmbeddedExtensionFactories({
      cfg,
      sessionManager,
      provider: "anthropic",
      modelId: "claude-sonnet-4-20250514",
      model,
      // agentId intentionally omitted
    });

    expect(factories).toContain(compactionSafeguardExtension);
    expect(getCompactionSafeguardRuntime(sessionManager)?.contextWindowTokens).toBe(200_000);
  });
});
