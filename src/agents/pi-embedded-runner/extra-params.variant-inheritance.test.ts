/**
 * Synthetic context-window variants must behave like the model they wrap.
 *
 * Regression cover for a defect found 2026-08-17: a variant is registered under its
 * own provider key ("anthropic-sonnet-1m" wrapping "anthropic"), and three separate
 * gates compared the provider with EXACT equality against "anthropic". The result was
 * that no sized Anthropic variant could ever receive a beta header or a default
 * cache retention, no matter how it was configured — `anthropic-sonnet-1m` could not
 * actually serve 1M, because the beta that unlocks it was unreachable by design.
 *
 * Verified against the live gateway at the time: a 243k-token prompt on
 * anthropic-sonnet-1m was rejected by Anthropic with "200000 maximum", because the
 * request went out with no context-1m beta header.
 */
import { describe, expect, it } from "vitest";

import type { OpenClawConfig } from "../../config/config.js";
import { resolveAnthropicBetas, resolveCacheRetention } from "./anthropic-stream-wrappers.js";
import { resolveExtraParams } from "./extra-params.js";

/** Config shaped like the real one: variant provider + base params only. */
function cfgWithVariant(baseParams: Record<string, unknown>): OpenClawConfig {
  return {
    models: {
      providers: {
        "anthropic-sonnet-1m": {
          models: [{ id: "claude-sonnet-4-5-1m", upstreamId: "claude-sonnet-4-5" }],
        },
        "google-flash-1m": {
          models: [{ id: "gemini-2.5-flash-1m", upstreamId: "gemini-2.5-flash" }],
        },
      },
    },
    agents: {
      defaults: {
        models: {
          "anthropic/claude-sonnet-4-5": { params: baseParams },
          "google/gemini-2.5-flash": { params: { someGoogleParam: true } },
        },
      },
    },
  } as unknown as OpenClawConfig;
}

describe("synthetic variant params inherit from the base model", () => {
  it("inherits base params when the variant has no entry of its own", () => {
    const params = resolveExtraParams({
      cfg: cfgWithVariant({ cacheRetention: "short", context1m: true }),
      provider: "anthropic-sonnet-1m",
      modelId: "claude-sonnet-4-5-1m",
    });
    expect(params).toEqual({ cacheRetention: "short", context1m: true });
  });

  it("an explicit variant entry still wins over the base", () => {
    const cfg = cfgWithVariant({ cacheRetention: "short" });
    (cfg as never as { agents: { defaults: { models: Record<string, unknown> } } }).agents.defaults.models[
      "anthropic-sonnet-1m/claude-sonnet-4-5-1m"
    ] = { params: { cacheRetention: "long" } };
    const params = resolveExtraParams({
      cfg,
      provider: "anthropic-sonnet-1m",
      modelId: "claude-sonnet-4-5-1m",
    });
    expect(params).toEqual({ cacheRetention: "long" });
  });

  it("a NON-variant provider is unaffected (no accidental fallback)", () => {
    const params = resolveExtraParams({
      cfg: cfgWithVariant({ cacheRetention: "short" }),
      provider: "anthropic",
      modelId: "claude-sonnet-4-5",
    });
    expect(params).toEqual({ cacheRetention: "short" });
  });

  it("returns undefined when the variant's upstreamId is missing", () => {
    const cfg = cfgWithVariant({ cacheRetention: "short" });
    (cfg as never as { models: { providers: Record<string, { models: unknown[] }> } }).models.providers[
      "anthropic-sonnet-1m"
    ].models = [{ id: "claude-sonnet-4-5-1m" }]; // no upstreamId
    expect(
      resolveExtraParams({ cfg, provider: "anthropic-sonnet-1m", modelId: "claude-sonnet-4-5-1m" }),
    ).toBeUndefined();
  });
});

describe("Anthropic gates accept synthetic variants", () => {
  it("resolves betas for a variant provider (was impossible before)", () => {
    expect(
      resolveAnthropicBetas({ context1m: true }, "anthropic-sonnet-1m", "claude-sonnet-4-5-1m"),
    ).toEqual(["context-1m-2025-08-07"]);
  });

  it("still resolves betas for the base provider", () => {
    expect(resolveAnthropicBetas({ context1m: true }, "anthropic", "claude-sonnet-4-5")).toEqual([
      "context-1m-2025-08-07",
    ]);
  });

  it("does NOT leak to a google variant — base 'google' is not Anthropic", () => {
    expect(
      resolveAnthropicBetas({ context1m: true }, "google-flash-1m", "gemini-2.5-flash-1m"),
    ).toBeUndefined();
  });

  it("gives a variant the same default cache retention as its base", () => {
    expect(resolveCacheRetention(undefined, "anthropic-sonnet-1m")).toBe("short");
    expect(resolveCacheRetention(undefined, "anthropic")).toBe("short");
  });

  it("does not give cache retention to a google variant", () => {
    expect(resolveCacheRetention(undefined, "google-flash-1m")).toBeUndefined();
  });
});
