import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ErrorCodes, errorShape } from "./error-codes.js";
import { ErrorShapeSchema } from "./frames.js";

/**
 * Every `ErrorCodes.X` the repo references must exist.
 *
 * TypeScript already says this, and said it about `INTERNAL_ERROR` in 51
 * places for as long as those call sites have existed. Nobody was running
 * `tsc` — the repo carries ~128 pre-existing errors — so the compiler was
 * telling the truth to an empty room.
 *
 * This test asserts the one invariant that has wire consequences, and it
 * runs in a second without the whole repo having to typecheck first. It is
 * not a replacement for the compiler; it is the part of the compiler's
 * answer that cannot be allowed to wait for the rest to be cleaned up.
 *
 * WHY IT MATTERS MORE THAN A MISSING LABEL. `errorShape` puts the value
 * straight into `{code, message}`. An undefined member serialises to a
 * frame with NO `code` key, and `ErrorShapeSchema.code` is a required
 * NonEmptyString — so the frame is invalid on the wire. A client that
 * validates (gateway/client.ts) discards it and never settles the pending
 * promise: a clean error becomes a hang until timeout.
 */

const SEARCH_ROOTS = ["src", "extensions"];
const SKIP_DIRS = new Set(["node_modules", "dist", "build", ".git"]);

function* tsFiles(dir: string): Generator<string> {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    let isDir = false;
    try {
      isDir = statSync(full).isDirectory();
    } catch {
      continue;
    }
    if (isDir) {
      yield* tsFiles(full);
    } else if (name.endsWith(".ts") || name.endsWith(".tsx")) {
      yield full;
    }
  }
}

function referencedCodes(): Map<string, string[]> {
  const found = new Map<string, string[]>();
  const repoRoot = join(import.meta.dirname, "..", "..", "..", "..");
  // This file necessarily writes the pattern out in prose to explain it, so
  // it matches itself. Skipping it by name rather than skipping all tests: a
  // test referencing a code that does not exist is still a real defect.
  const selfName = "error-codes.test.ts";
  for (const root of SEARCH_ROOTS) {
    for (const file of tsFiles(join(repoRoot, root))) {
      if (file.endsWith(selfName)) continue;
      const src = readFileSync(file, "utf-8");
      for (const m of src.matchAll(/ErrorCodes\.([A-Z_]+)/g)) {
        const list = found.get(m[1]) ?? [];
        if (!list.includes(file)) list.push(file);
        found.set(m[1], list);
      }
    }
  }
  return found;
}

describe("ErrorCodes", () => {
  it("defines every member the repo references", () => {
    const missing: string[] = [];
    for (const [code, files] of referencedCodes()) {
      if (!(code in ErrorCodes)) {
        missing.push(`${code} (used in ${files.length} file(s), e.g. ${files[0]})`);
      }
    }
    expect(
      missing,
      "these evaluate to undefined at runtime, and an error frame with no " +
        "`code` fails ErrorShapeSchema — a validating client discards it and " +
        "never settles its promise",
    ).toEqual([]);
  });

  it("produces a frame that satisfies ErrorShapeSchema for every code", () => {
    // The property the missing member broke, asserted directly rather than
    // inferred from the type: `code` is a required NonEmptyString.
    const required = (ErrorShapeSchema as { required?: string[] }).required ?? [];
    expect(required).toContain("code");

    for (const code of Object.values(ErrorCodes)) {
      const shape = errorShape(code, "something went wrong");
      expect(shape.code, `${code} produced an empty code`).toBeTruthy();
      expect(typeof shape.code).toBe("string");
    }
  });

  it("keeps INTERNAL_ERROR, which 51 call sites already assumed", () => {
    // Named explicitly so that removing it fails loudly here rather than
    // silently in every handler that reports an unexpected failure.
    expect(ErrorCodes.INTERNAL_ERROR).toBe("INTERNAL_ERROR");
  });
});
