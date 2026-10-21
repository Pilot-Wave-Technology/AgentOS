import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OpenClawConfig } from "../../config/config.js";
import {
  DEFAULT_MEMORY_FLUSH_PROMPT,
  resolveMemoryFlushContextWindowTokens,
  resolveMemoryFlushPromptForRun,
  resolveMemoryFlushRelativePathForRun,
} from "./memory-flush.js";
import { lookupContextTokens } from "../../agents/context.js";
import { DEFAULT_CONTEXT_TOKENS } from "../../agents/defaults.js";

vi.mock("../../agents/context.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../agents/context.js")>();
  return {
    ...actual,
    lookupContextTokens: vi.fn(),
  };
});

describe("resolveMemoryFlushPromptForRun", () => {
  const cfg = {
    agents: {
      defaults: {
        userTimezone: "America/New_York",
        timeFormat: "12",
      },
    },
  } as OpenClawConfig;

  it("replaces YYYY-MM-DD using user timezone and appends current time", () => {
    const prompt = resolveMemoryFlushPromptForRun({
      prompt: "Store durable notes in memory/YYYY-MM-DD.md",
      cfg,
      nowMs: Date.UTC(2026, 1, 16, 15, 0, 0),
    });

    expect(prompt).toContain("memory/2026-02-16.md");
    expect(prompt).toContain(
      "Current time: Monday, February 16th, 2026 — 10:00 AM (America/New_York) / 2026-02-16 15:00 UTC",
    );
  });

  it("does not append a duplicate current time line", () => {
    const prompt = resolveMemoryFlushPromptForRun({
      prompt: "Store notes.\nCurrent time: already present",
      cfg,
      nowMs: Date.UTC(2026, 1, 16, 15, 0, 0),
    });

    expect(prompt).toContain("Current time: already present");
    expect((prompt.match(/Current time:/g) ?? []).length).toBe(1);
  });

  it("resolves the canonical relative memory path using user timezone", () => {
    const relativePath = resolveMemoryFlushRelativePathForRun({
      cfg,
      nowMs: Date.UTC(2026, 1, 16, 15, 0, 0),
    });

    expect(relativePath).toBe("memory/2026-02-16.md");
  });
});

describe("DEFAULT_MEMORY_FLUSH_PROMPT", () => {
  it("includes append-only instruction to prevent overwrites (#6877)", () => {
    expect(DEFAULT_MEMORY_FLUSH_PROMPT).toMatch(/APPEND/i);
    // Prompt wording says "never overwrite" (READ_ONLY_HINT) — same semantic
    // as the historical "do not overwrite" phrasing.
    expect(DEFAULT_MEMORY_FLUSH_PROMPT).toMatch(/never overwrite|do not overwrite/i);
  });

  it("includes anti-fragmentation instruction to prevent timestamped variant files (#34919)", () => {
    // Agents must not create YYYY-MM-DD-HHMM.md variants alongside the canonical file
    expect(DEFAULT_MEMORY_FLUSH_PROMPT).toContain("timestamped variant");
    expect(DEFAULT_MEMORY_FLUSH_PROMPT).toContain("YYYY-MM-DD.md");
  });
});

describe("resolveMemoryFlushContextWindowTokens (acd626a5)", () => {
  beforeEach(() => {
    vi.mocked(lookupContextTokens).mockReset();
  });

  it("per-agent contextTokens clamps downward against a 1M model window", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(1_000_000);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "gemini/flash-1m",
        agentCfgContextTokens: 32_000,
      }),
    ).toBe(32_000);
  });

  it("per-agent contextTokens wins over a larger global default", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(200_000);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "anthropic/claude-opus-4-5",
        agentCfgContextTokens: 50_000,
        cfg: {
          agents: { defaults: { contextTokens: 100_000 } },
        } as OpenClawConfig,
      }),
    ).toBe(50_000);
  });

  it("per-agent contextTokens wins even when the global default is smaller", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(200_000);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "anthropic/claude-opus-4-5",
        agentCfgContextTokens: 100_000,
        cfg: {
          agents: { defaults: { contextTokens: 50_000 } },
        } as OpenClawConfig,
      }),
    ).toBe(100_000);
  });

  it("per-agent contextTokens above the model window returns the model window (clamp-only)", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(200_000);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "anthropic/claude-opus-4-5",
        agentCfgContextTokens: 500_000,
      }),
    ).toBe(200_000);
  });

  it("falls back to agents.defaults.contextTokens when per-agent is unset (backward-compat)", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(200_000);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "anthropic/claude-opus-4-5",
        cfg: {
          agents: { defaults: { contextTokens: 32_000 } },
        } as OpenClawConfig,
      }),
    ).toBe(32_000);
  });

  it("returns DEFAULT_CONTEXT_TOKENS when everything is undefined", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(undefined);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "unknown/model",
      }),
    ).toBe(DEFAULT_CONTEXT_TOKENS);
  });

  it("uses DEFAULT_CONTEXT_TOKENS as base and clamps per-agent below it when the model is unknown", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(undefined);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "unknown/model",
        agentCfgContextTokens: 32_000,
      }),
    ).toBe(32_000);
  });

  it("ignores zero and negative per-agent values (normalizePositiveInt)", () => {
    vi.mocked(lookupContextTokens).mockReturnValue(200_000);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "m",
        agentCfgContextTokens: 0,
        cfg: { agents: { defaults: { contextTokens: 50_000 } } } as OpenClawConfig,
      }),
    ).toBe(50_000);
    expect(
      resolveMemoryFlushContextWindowTokens({
        modelId: "m",
        agentCfgContextTokens: -1,
      }),
    ).toBe(200_000);
  });
});
