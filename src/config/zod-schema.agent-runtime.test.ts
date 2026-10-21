import { describe, expect, it } from "vitest";
import { AgentEntrySchema } from "./zod-schema.agent-runtime.js";

describe("AgentEntrySchema contextTokens (cb11f6b9)", () => {
  it("accepts a positive integer contextTokens and preserves the value", () => {
    const parsed = AgentEntrySchema.parse({ id: "a", contextTokens: 32_000 });
    expect(parsed.contextTokens).toBe(32_000);
  });

  it("accepts an agent entry without contextTokens (optional)", () => {
    const parsed = AgentEntrySchema.parse({ id: "a" });
    expect(parsed.contextTokens).toBeUndefined();
  });

  it("rejects contextTokens: 0 (positive-int)", () => {
    expect(() => AgentEntrySchema.parse({ id: "a", contextTokens: 0 })).toThrow(
      /contextTokens|positive|greater/i,
    );
  });

  it("rejects contextTokens: -1 (positive-int)", () => {
    expect(() => AgentEntrySchema.parse({ id: "a", contextTokens: -1 })).toThrow(
      /contextTokens|positive|greater/i,
    );
  });

  it("rejects contextTokens: 1.5 (int)", () => {
    expect(() => AgentEntrySchema.parse({ id: "a", contextTokens: 1.5 })).toThrow(
      /contextTokens|integer|int/i,
    );
  });

  it("rejects contextTokens: 'lots' (type)", () => {
    expect(() => AgentEntrySchema.parse({ id: "a", contextTokens: "lots" })).toThrow(
      /contextTokens|number|expected/i,
    );
  });

  it("still .strict()-rejects unknown keys on the same entry", () => {
    expect(() => AgentEntrySchema.parse({ id: "a", unknownKey: 1 })).toThrow(
      /unrecognized|unknown|unknownKey/i,
    );
  });
});
