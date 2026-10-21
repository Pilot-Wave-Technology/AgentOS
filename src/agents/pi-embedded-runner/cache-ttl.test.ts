import { describe, expect, it } from "vitest";
import { isCacheTtlEligibleProvider } from "./cache-ttl.js";

describe("isCacheTtlEligibleProvider", () => {
  it("allows anthropic", () => {
    expect(isCacheTtlEligibleProvider("anthropic", "claude-sonnet-4-20250514")).toBe(true);
  });

  it("allows synthetic context-window variants of native providers (base-provider match)", () => {
    // Synthetic variants are registered under their own provider keys
    // (models-synthetic.json); eligibility comes from the base provider
    // (segment before the first "-").
    expect(isCacheTtlEligibleProvider("anthropic-sonnet-100k", "claude-sonnet-4-5-100k")).toBe(true);
    expect(isCacheTtlEligibleProvider("anthropic-opus-1m", "claude-opus-4-8-1m")).toBe(true);
    expect(isCacheTtlEligibleProvider("moonshot-200k", "kimi-k2.5")).toBe(true);
  });

  it("rejects google synthetic variants (Gemini is not a cache-ttl provider)", () => {
    expect(isCacheTtlEligibleProvider("google-flash-50k", "gemini-2.5-flash-50k")).toBe(false);
    expect(isCacheTtlEligibleProvider("google-pro-200k", "gemini-2.5-pro-200k")).toBe(false);
  });

  it("allows moonshot and zai providers", () => {
    expect(isCacheTtlEligibleProvider("moonshot", "kimi-k2.5")).toBe(true);
    expect(isCacheTtlEligibleProvider("zai", "glm-5")).toBe(true);
  });

  it("is case-insensitive for native providers", () => {
    expect(isCacheTtlEligibleProvider("Moonshot", "Kimi-K2.5")).toBe(true);
    expect(isCacheTtlEligibleProvider("ZAI", "GLM-5")).toBe(true);
  });

  it("allows openrouter cache-ttl models", () => {
    expect(isCacheTtlEligibleProvider("openrouter", "anthropic/claude-sonnet-4")).toBe(true);
    expect(isCacheTtlEligibleProvider("openrouter", "moonshotai/kimi-k2.5")).toBe(true);
    expect(isCacheTtlEligibleProvider("openrouter", "moonshot/kimi-k2.5")).toBe(true);
    expect(isCacheTtlEligibleProvider("openrouter", "zai/glm-5")).toBe(true);
  });

  it("rejects unsupported providers and models", () => {
    expect(isCacheTtlEligibleProvider("openai", "gpt-4o")).toBe(false);
    expect(isCacheTtlEligibleProvider("openrouter", "openai/gpt-4o")).toBe(false);
  });
});
