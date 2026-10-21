import { afterEach, describe, expect, it, vi } from "vitest";

describe("misc model overrides", () => {
  afterEach(async () => {
    const mod = await import("./misc-model-overrides.js");
    mod.clearMiscModelOverrides();
  });

  it("returns the fallback when a purpose has no override", async () => {
    const { getMiscModelOverride } = await import("./misc-model-overrides.js");
    expect(getMiscModelOverride("gateway_compaction", "google/gemini-3.8-flash")).toBe(
      "google/gemini-3.8-flash",
    );
  });

  it("a value written by one copy of the module is read by another", async () => {
    // The extension and core can load separate copies of this module; the
    // store must be shared or every override silently does nothing.
    const writer = await import("./misc-model-overrides.js");
    writer.setMiscModelOverrides({ gateway_compaction: "anthropic/claude-sonnet-5-5" });
    vi.resetModules();
    const reader = await import("./misc-model-overrides.js");
    expect(reader).not.toBe(writer);
    expect(reader.getMiscModelOverride("gateway_compaction", "x")).toBe(
      "anthropic/claude-sonnet-5-5",
    );
    expect(reader.getMiscModelOverrides()).toEqual({
      gateway_compaction: "anthropic/claude-sonnet-5-5",
    });
  });
});
