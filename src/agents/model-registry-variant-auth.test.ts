import { describe, expect, it } from "vitest";
import {
  resolveVariantEnvApiKey,
  resolveVariantUpstreamProviderId,
} from "./model-registry-variant-auth.js";

/**
 * These tests pin the two safety properties the fork hook in
 * `model-auth.ts::resolveEnvApiKey` depends on. If either breaks, that hook
 * stops being a safe last-resort addition and starts being able to hand a
 * provider someone else's credential — so treat a failure here as a security
 * regression, not a cosmetic one.
 */
describe("resolveVariantUpstreamProviderId", () => {
  it("resolves every shape the registry's emitter actually produces", () => {
    // Real ids taken from a deployed gateway's models.providers.
    expect(resolveVariantUpstreamProviderId("anthropic-sonnet-200k")).toBe("anthropic");
    expect(resolveVariantUpstreamProviderId("anthropic-sonnet-50k")).toBe("anthropic");
    expect(resolveVariantUpstreamProviderId("anthropic-opus48-500k")).toBe("anthropic");
    expect(resolveVariantUpstreamProviderId("anthropic-sonnet5-100k")).toBe("anthropic");
    expect(resolveVariantUpstreamProviderId("google-flash-1m")).toBe("google");
    expect(resolveVariantUpstreamProviderId("google-pro-500k")).toBe("google");
  });

  it("does NOT fire for ordinary provider ids", () => {
    // The whole point of requiring a trailing size segment: a naive
    // "strip the last hyphenated segment" fallback would resolve
    // `openai-compatible` to `openai` and silently hand it the wrong key.
    for (const id of [
      "anthropic",
      "google",
      "openai",
      "openai-compatible",
      "claude-cli",
      "google-vertex",
      "vercel-ai-gateway",
      "cloudflare-ai-gateway",
      "minimax-portal",
    ]) {
      expect(resolveVariantUpstreamProviderId(id)).toBeNull();
    }
  });

  it("rejects near-misses that only look like a variant id", () => {
    expect(resolveVariantUpstreamProviderId("anthropic-sonnet-200")).toBeNull(); // no k/m unit
    expect(resolveVariantUpstreamProviderId("anthropic-sonnet-xl")).toBeNull(); // not a size
    expect(resolveVariantUpstreamProviderId("anthropic-200k")).toBeNull(); // no shorthand segment
    expect(resolveVariantUpstreamProviderId("200k")).toBeNull();
    expect(resolveVariantUpstreamProviderId("")).toBeNull();
  });

  it("keeps a hyphenated upstream intact (greedy leading group)", () => {
    expect(resolveVariantUpstreamProviderId("foo-bar-baz-100k")).toBe("foo-bar");
  });
});

describe("resolveVariantEnvApiKey", () => {
  const candidates = (upstream: string): readonly string[] | undefined =>
    ({ anthropic: ["ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"] })[upstream];

  it("resolves a variant through its upstream's candidates, in order", () => {
    const seen: string[] = [];
    const result = resolveVariantEnvApiKey("anthropic-sonnet-200k", candidates, (envVar) => {
      seen.push(envVar);
      return envVar === "ANTHROPIC_API_KEY" ? "sk-real" : null;
    });
    expect(result).toBe("sk-real");
    // Order matters: the OAuth candidate is tried first, exactly as stock
    // resolution would for the real `anthropic` provider.
    expect(seen).toEqual(["ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]);
  });

  it("returns null for a non-variant id without consulting anything", () => {
    let consulted = false;
    const result = resolveVariantEnvApiKey(
      "openai-compatible",
      (u) => {
        consulted = true;
        return candidates(u);
      },
      () => "should-never-be-reached",
    );
    expect(result).toBeNull();
    expect(consulted).toBe(false);
  });

  it("returns null when the upstream has no candidates at all", () => {
    expect(resolveVariantEnvApiKey("mystery-thing-200k", candidates, () => "x")).toBeNull();
  });

  it("returns null when every candidate env var is unset", () => {
    expect(resolveVariantEnvApiKey("anthropic-sonnet-200k", candidates, () => null)).toBeNull();
  });
});
