/**
 * An agent's threads must not rewrite its MEMORY.md at the same time.
 *
 * THE CASE THIS IS ABOUT. `resolveAgentWorkspaceDir` takes an agent id
 * and no session, so one agent has one workspace and one MEMORY.md,
 * while the session key `agent:{agent}:{goal}-{thread}` gives it one
 * turn loop per thread. A flush appends and is safe. A consolidation
 * rewrites the file whole: two at once and the slower one writes back a
 * document assembled from a read that predates the other's rewrite.
 */
import { describe, expect, it, beforeEach } from "vitest";

import {
  __resetConsolidationLocks,
  acquireConsolidationLock,
  isConsolidating,
  withConsolidationLock,
} from "./memory-consolidation-lock.js";

beforeEach(() => {
  __resetConsolidationLocks();
});

describe("the second thread does not get in", () => {
  it("refuses a workspace somebody is already consolidating", () => {
    const first = acquireConsolidationLock("/ws/go-KER12");
    expect(first).not.toBeNull();
    expect(acquireConsolidationLock("/ws/go-KER12")).toBeNull();
  });

  it("lets the next one in once the holder is done", () => {
    const first = acquireConsolidationLock("/ws/go-KER12");
    first?.();
    expect(acquireConsolidationLock("/ws/go-KER12")).not.toBeNull();
  });

  it("locks per workspace, not globally", () => {
    // Two goals consolidating at once is fine and must stay fine --
    // a global lock would serialise every agent on the host behind
    // whichever one happened to be rewriting.
    acquireConsolidationLock("/ws/go-KER12");
    expect(acquireConsolidationLock("/ws/go-MARL2")).not.toBeNull();
  });
});

describe("the lock is never stranded", () => {
  it("releases when the run throws", async () => {
    const outcome = withConsolidationLock("/ws/go-KER12", async () => {
      throw new Error("model refused");
    });
    await expect(outcome).rejects.toThrow("model refused");
    expect(isConsolidating("/ws/go-KER12")).toBe(false);
  });

  it("a double release cannot free a LATER holder's lock", () => {
    // The hazard is a retry path calling release twice: the second call
    // would drop a lock a different turn has since taken, and two
    // rewrites would run with no warning at all.
    const first = acquireConsolidationLock("/ws/go-KER12");
    first?.();
    const second = acquireConsolidationLock("/ws/go-KER12");
    expect(second).not.toBeNull();
    first?.();
    expect(isConsolidating("/ws/go-KER12")).toBe(true);
  });
});

describe("skipping is reported, not disguised as success", () => {
  it("says it did not run", async () => {
    acquireConsolidationLock("/ws/go-KER12");
    const result = await withConsolidationLock("/ws/go-KER12", async () => 1);
    expect(result.ran).toBe(false);
  });

  it("hands back the value when it did run", async () => {
    const result = await withConsolidationLock("/ws/go-KER12", async () => 1);
    expect(result).toEqual({ ran: true, value: 1 });
  });

  it("an unresolvable workspace still runs", async () => {
    // Not consolidating a shared file, so refusing would turn "I don't
    // know where this is" into "this never runs".
    const result = await withConsolidationLock(undefined, async () => 1);
    expect(result.ran).toBe(true);
  });
});
