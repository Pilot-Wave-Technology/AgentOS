import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `scripts/redeploy.sh` sends large blocks to the gateway host as ONE
 * double-quoted argument:
 *
 *     sudo -u "$GATEWAY_USER" -H bash -c "
 *       ... a hundred lines of config set ...
 *     "
 *
 * Inside those quotes the OUTER shell expands backticks as legacy
 * command substitution before the inner shell sees anything, and a `#`
 * comment is not a comment to the parser doing that expansion.
 *
 * WHAT IT COST. A comment added in 34341d9 read
 *
 *     # logged `budget=100000 preserved=181 (~99141 tokens)` and ...
 *
 * so the shell tried to RUN `budget=100000 ...` and died on the open
 * paren. The deploy did not fail -- it logged one line of syntax error
 * among hundreds and reported success -- and every
 * `agents.defaults.compaction.*` setting in that block silently stopped
 * being applied, including the `preservedToolResultMaxTokens` that the
 * same commit was adding. Two commits of compaction tuning sat in the
 * repository and never reached a gateway.
 *
 * WHY A TEST AND NOT CARE. `bash -n scripts/redeploy.sh` exits 0: the
 * contents of a command substitution are parsed when it RUNS, not when
 * the file is read, so the obvious check cannot see this. And the
 * repository's comment style is to wrap identifiers in backticks
 * everywhere else in the same file, which makes writing one here the
 * natural mistake rather than a careless one.
 */
const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const SCRIPT = resolve(REPO, "scripts/redeploy.sh");

/** The [start, end] line numbers of every `bash -c "` ... `"` block. */
function quotedBlocks(lines: string[]): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (let i = 0; i < lines.length; i++) {
    if (!/bash\s+-c\s+"\s*$/.test(lines[i])) continue;
    for (let j = i + 1; j < lines.length; j++) {
      const t = lines[j].trim();
      if (t === '"' || t === '";') {
        out.push([i, j]);
        i = j;
        break;
      }
    }
  }
  return out;
}

describe("redeploy.sh heredoc-style quoting", () => {
  const lines = readFileSync(SCRIPT, "utf8").split("\n");
  const blocks = quotedBlocks(lines);

  it("finds the double-quoted bash -c blocks at all", () => {
    // If this fails the guard below is vacuous — it would pass by
    // having nothing to check, which is the failure mode every other
    // check in this area has had.
    expect(blocks.length).toBeGreaterThan(0);
  });

  it("has no backtick inside a double-quoted bash -c block", () => {
    const offenders: string[] = [];
    for (const [start, end] of blocks) {
      for (let n = start; n <= end; n++) {
        if (lines[n].includes("`")) {
          offenders.push(`line ${n + 1}: ${lines[n].trim().slice(0, 90)}`);
        }
      }
    }
    expect(offenders, [
      "A backtick inside these quotes is command substitution run by the",
      "OUTER shell — including in a `#` comment. It does not fail the",
      "deploy; it kills the rest of the block and the build stays green.",
      "Use single quotes in comments inside these blocks.",
    ].join("\n")).toEqual([]);
  });

  it("has no unescaped double quote inside a double-quoted bash -c block", () => {
    // THE ONE THAT ACTUALLY BROKE A DEPLOY, and the one the first
    // version of this guard did not check.
    //
    // A pair of quotes in a comment does not look dangerous — it looks
    // balanced. But each one closes and reopens the OUTER string, so
    // the words between them become bare shell tokens. a2bd211 wrote
    //
    //     # gemini-2.5-flash "for cost" while the agents ...
    //     # keepRecentMode="turns" (legacy/default). When ...
    //
    // and `(legacy/default)` — now unquoted — produced
    // `syntax error near unexpected token '('`. Everything below those
    // comments fell outside the block, including the one `config set`
    // that commit was adding. The gateway kept `compaction.model` on
    // `google/gemini-2.5-flash` for two days while the repository said
    // `gemini-3.8-flash-500k`, and three deploys reported SUCCESS.
    //
    // Use single quotes in comments inside these blocks.
    const offenders: string[] = [];
    for (const [start, end] of blocks) {
      for (let n = start + 1; n < end; n++) {
        if (/(?<!\\)"/.test(lines[n])) {
          offenders.push(`line ${n + 1}: ${lines[n].trim().slice(0, 90)}`);
        }
      }
    }
    expect(offenders, "a double quote here ends the bash -c argument early")
      .toEqual([]);
  });

  it("has no unescaped $( inside a double-quoted bash -c block", () => {
    // Same mechanism, the modern spelling. `\$(` is fine — that is
    // deliberately deferred to the inner shell.
    const offenders: string[] = [];
    for (const [start, end] of blocks) {
      for (let n = start; n <= end; n++) {
        if (/(^|[^\\$])\$\(/.test(lines[n])) {
          offenders.push(`line ${n + 1}: ${lines[n].trim().slice(0, 90)}`);
        }
      }
    }
    expect(offenders, "write \\$( to defer the substitution to the inner shell")
      .toEqual([]);
  });
});
