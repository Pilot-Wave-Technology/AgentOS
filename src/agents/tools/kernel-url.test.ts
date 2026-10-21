import { describe, expect, it } from "vitest";

import { kernelConfigured, resolveKernelToken, resolveKernelUrl } from "./kernel-url.js";

describe("resolveKernelUrl", () => {
  it("derives the surface from CONTROL_PANEL_URL when nothing is set", () => {
    // THE CASE EVERY DEPLOYED GATEWAY WAS IN. Nothing in this repository
    // ever wrote KERNEL_ASSISTANT_URL, so every kernel tool refused.
    expect(resolveKernelUrl({ CONTROL_PANEL_URL: "https://cp.example" }))
      .toBe("https://cp.example/internal/kernel");
  });

  it("prefers an explicit KERNEL_ASSISTANT_URL", () => {
    // E0 and local development point this at 127.0.0.1:8000.
    expect(
      resolveKernelUrl({
        KERNEL_ASSISTANT_URL: "http://127.0.0.1:8000/internal/kernel",
        CONTROL_PANEL_URL: "https://cp.example",
      }),
    ).toBe("http://127.0.0.1:8000/internal/kernel");
  });

  it("does not double the slash on a trailing-slash base", () => {
    expect(resolveKernelUrl({ CONTROL_PANEL_URL: "https://cp.example/" }))
      .toBe("https://cp.example/internal/kernel");
  });

  it("is empty when neither is set, which is what E_NO_KERNEL reports", () => {
    expect(resolveKernelUrl({})).toBe("");
    expect(kernelConfigured({})).toBe(false);
  });

  it("treats a blank variable as unset rather than as a base", () => {
    // startup.sh.tpl writes `CONTROL_PANEL_URL=` at boot and the refresh
    // timer fills it in later; an empty string must not become "/internal/kernel".
    expect(resolveKernelUrl({ CONTROL_PANEL_URL: "  ", KERNEL_ASSISTANT_URL: "" }))
      .toBe("");
  });

  it("agrees with the tools about whether the kernel is reachable", () => {
    // kernelSupersedes() drops the legacy cancel_story/register_resource/
    // grant_resource twins on this answer. If it said yes where the tools
    // say no -- or the reverse -- one name is declared twice and the
    // provider rejects every turn.
    const env = { CONTROL_PANEL_URL: "https://cp.example" };
    expect(kernelConfigured(env)).toBe(resolveKernelUrl(env) !== "");
  });
});

describe("resolveKernelToken", () => {
  it("falls back to the gateway's shared token", () => {
    // The surface refuses to mount without ASSISTANT_API_TOKEN now, and
    // the deployment feeds it the same secret. Sending nothing would
    // turn every tool into E_UNAUTHORISED.
    expect(resolveKernelToken({ OPENCLAW_GATEWAY_TOKEN: "shared" })).toBe("shared");
  });

  it("prefers an explicit KERNEL_ASSISTANT_TOKEN", () => {
    expect(
      resolveKernelToken({
        KERNEL_ASSISTANT_TOKEN: "own",
        OPENCLAW_GATEWAY_TOKEN: "shared",
      }),
    ).toBe("own");
  });

  it("is empty when neither is set", () => {
    expect(resolveKernelToken({})).toBe("");
  });
});
