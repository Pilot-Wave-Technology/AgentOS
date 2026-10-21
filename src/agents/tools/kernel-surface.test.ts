import { describe, expect, it } from "vitest";

import { kernelSurfaceFor } from "./kernel-url.js";

/**
 * A tool must serve the agent CALLING it.
 *
 * WHAT WENT WRONG. `kernel-assistant-tools.ts` and `kernel-go-tools.ts`
 * both declare `ask_executor`, `steer`, `story_progress`,
 * `story_status` and `cancel_story`. That sharing is deliberate — the
 * kernel's rule is "shared FUNCTION, separate AUTHORITY", enforced per
 * call by `go_may_drive`. But `openclaw-tools.ts` spreads both sets
 * into ONE flat array keyed by name, GO's second, so the GO's
 * definition won every colliding name and its hard-coded `/go/` path
 * served every agent.
 *
 * Measured on dev, goal AGO01: three `POST /internal/kernel/go/ago01/
 * ask_executor` and zero to `/assistant/...`, while the only calls that
 * did reach the assistant surface were `start_story` and
 * `list_resources` — the two names that DO NOT collide. The assistant
 * was refused with E_NOT_AUTONOMOUS, a message about the GO, and wrote
 * it into its durable memory as a rule about itself.
 */
describe("kernelSurfaceFor", () => {
  it("routes a goal orchestrator to the GO surface", () => {
    expect(kernelSurfaceFor("go-ago01", "ago01")).toBe("go");
  });

  it("is case-insensitive, because the gateway lowercases agent ids", () => {
    // The goal is AGO01; the gateway gives its orchestrator `go-ago01`.
    expect(kernelSurfaceFor("go-ago01", "AGO01")).toBe("go");
    expect(kernelSurfaceFor("GO-AGO01", "ago01")).toBe("go");
  });

  it("routes an assistant to the assistant surface", () => {
    // The exact id from the dev goal where this was found.
    expect(kernelSurfaceFor("abhishek-chaudhary-assistant-0cfb", "ago01"))
      .toBe("assistant");
  });

  it("does not hand the GO surface to an agent merely NAMED like one", () => {
    // Equality, not a `go-` prefix. The kernel's own definition is
    // `lower(id) = lower('go-' || goal_id)` (app/ledger/actors.py), and
    // a prefix test would claim `go-somewhere-else` on any goal.
    expect(kernelSurfaceFor("go-othergoal", "ago01")).toBe("assistant");
    expect(kernelSurfaceFor("go-ago01-worker", "ago01")).toBe("assistant");
    expect(kernelSurfaceFor("going-places", "ago01")).toBe("assistant");
  });

  it("falls back to the assistant surface when it cannot tell", () => {
    // The SAFE direction. `/assistant/` grants nothing the caller does
    // not already have; `/go/` is the gated one. Guessing `go` for an
    // unknown caller is how the original bug behaved.
    expect(kernelSurfaceFor("", "ago01")).toBe("assistant");
    expect(kernelSurfaceFor("go-ago01", "")).toBe("assistant");
    expect(kernelSurfaceFor("", "")).toBe("assistant");
    expect(kernelSurfaceFor(undefined as never, undefined as never))
      .toBe("assistant");
  });

  it("tolerates whitespace in either argument", () => {
    expect(kernelSurfaceFor("  go-ago01  ", " ago01 ")).toBe("go");
  });
});
