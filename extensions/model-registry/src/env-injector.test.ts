import { afterEach, describe, expect, it } from "vitest";
import { clearInjectedApiKeysExcept, injectResolvedApiKey } from "./env-injector.js";

const ENV_VARS_TO_CLEAN = ["ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"];

afterEach(() => {
  // Real bridge into real env vars — never leak a test value into
  // another test or the actual process environment.
  for (const key of ENV_VARS_TO_CLEAN) {
    delete process.env[key];
  }
  // Reset the module's internal tracking set too, so each test starts
  // from a clean slate regardless of injection/clear order.
  clearInjectedApiKeysExcept([]);
});

describe("injectResolvedApiKey", () => {
  it("sets the known env var for a recognized provider id", () => {
    const result = injectResolvedApiKey("anthropic", "sk-test-value");
    expect(result).toBe(true);
    expect(process.env.ANTHROPIC_API_KEY).toBe("sk-test-value");
  });

  it("returns false and injects nothing for an unrecognized provider id", () => {
    const result = injectResolvedApiKey("some-custom-cli-tool", "sk-test-value");
    expect(result).toBe(false);
    expect(process.env.SOME_CUSTOM_CLI_TOOL_API_KEY).toBeUndefined();
  });

  it("overwrites a previously-injected value for the same provider", () => {
    injectResolvedApiKey("anthropic", "sk-old");
    injectResolvedApiKey("anthropic", "sk-new");
    expect(process.env.ANTHROPIC_API_KEY).toBe("sk-new");
  });
});

describe("clearInjectedApiKeysExcept", () => {
  it("clears an env var for a provider no longer in the keep-set", () => {
    injectResolvedApiKey("anthropic", "sk-test-value");
    clearInjectedApiKeysExcept([]); // anthropic dropped out (e.g. revoked)
    expect(process.env.ANTHROPIC_API_KEY).toBeUndefined();
  });

  it("leaves an env var alone for a provider still in the keep-set", () => {
    injectResolvedApiKey("anthropic", "sk-test-value");
    clearInjectedApiKeysExcept(["anthropic"]);
    expect(process.env.ANTHROPIC_API_KEY).toBe("sk-test-value");
  });

  it("clears only the dropped provider, not others still present", () => {
    injectResolvedApiKey("anthropic", "sk-anthropic");
    injectResolvedApiKey("google", "sk-google");
    clearInjectedApiKeysExcept(["google"]); // anthropic dropped, google kept
    expect(process.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(process.env.GEMINI_API_KEY).toBe("sk-google");
  });

  it("is a no-op when nothing has ever been injected", () => {
    expect(() => clearInjectedApiKeysExcept([])).not.toThrow();
  });

  it("clearing twice in a row is safe (idempotent)", () => {
    injectResolvedApiKey("anthropic", "sk-test-value");
    clearInjectedApiKeysExcept([]);
    expect(() => clearInjectedApiKeysExcept([])).not.toThrow();
    expect(process.env.ANTHROPIC_API_KEY).toBeUndefined();
  });
});
