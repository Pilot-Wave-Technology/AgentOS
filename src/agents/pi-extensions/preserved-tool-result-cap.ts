import type { AgentMessage } from "@mariozechner/pi-agent-core";

// The SAME estimator the budget walk uses. A cap measured by a different
// ruler than the budget it defends would be a second source of drift.
import { estimateMessagesTokens } from "../compaction.js";

/**
 * Cap the SIZE of tool results that compaction preserves, without dropping
 * the messages that carry them.
 *
 * WHY THIS EXISTS. `splitPreservedRecentTurnsByTokens` walks backward
 * accumulating `estimateMessagesTokens` until `keepRecentTokensBudget` is
 * exhausted — and then a second pass adds every `toolResult` that pairs with
 * a preserved assistant turn, WITHOUT adding to `runningTokens`. Those
 * results are preserved, uncounted and unbudgeted: the budget never applied
 * to them and the log line never saw them.
 *
 * Measured on a goal orchestrator (2026-09-30 19:50): the safeguard logged
 * `budget=100000 preserved=181 messages (~99141 tokens)` and the session's
 * next call carried 212,303 input tokens against a ~29K fixed prefix. The
 * ~84K difference is paired tool results. An earlier split the same evening
 * preserved 16 messages for ~149,545 tokens — 9.3K per message.
 *
 * THE MESSAGES ARE THE POINT, NOT THE PAYLOADS. The conversation shape —
 * user turns, assistant reasoning, which tools were called — is what the
 * model needs to carry on. The ten thousand tokens of file content a `read`
 * returned is what makes it expensive. So this keeps every message, its
 * role, its tool name and its id, and shortens only the text inside an
 * oversized result, leaving a marker that says what went.
 *
 * Modelled on `pruneProcessedHistoryImages`, which does the same thing for
 * image blocks: replace the block, keep the message.
 *
 * NEVER EMPTIES A RESULT. A `toolResult` with no content is rejected by
 * providers that validate tool_use/tool_result pairing, which would turn a
 * context optimisation into a hard API failure on the next turn. Every
 * capped result keeps a head and a marker.
 */

/** Said in the transcript where the bytes were. Includes the count so a
 *  reader (and the model) can tell a trim from a tool that returned little. */
export function elisionMarker(droppedTokens: number): string {
  return `\n\n[… ~${droppedTokens.toLocaleString()} tokens of tool output elided at compaction. `
    + "The full result is in this story's history; re-run the tool if you need it again. …]";
}

/** How much of an over-cap result to keep, as a fraction of the cap. The
 *  rest of the cap is left for the marker and estimator slack. */
const HEAD_SHARE = 0.9;

/** Rough chars-per-token for trimming text. Deliberately conservative (real
 *  English is ~4): overshooting the trim is safe, undershooting leaves the
 *  result above cap. */
const CHARS_PER_TOKEN = 3;

function textOf(block: unknown): string | null {
  if (!block || typeof block !== "object") return null;
  const b = block as { type?: unknown; text?: unknown };
  if (b.type !== "text" || typeof b.text !== "string") return null;
  return b.text;
}

/**
 * Return `messages` with oversized `toolResult` bodies shortened.
 *
 * `maxTokens <= 0` disables capping and returns the input unchanged — which
 * is the default, so a deployment that sets nothing behaves exactly as it
 * did before.
 */
export function capPreservedToolResults(
  messages: AgentMessage[],
  maxTokens: number,
  maxTotalTokens = 0,
): { messages: AgentMessage[]; cappedCount: number; tokensDropped: number;
     elidedCount: number } {
  if (!Number.isFinite(maxTokens) || maxTokens <= 0 || !messages.length) {
    return { messages, cappedCount: 0, tokensDropped: 0, elidedCount: 0 };
  }

  // WHICH RESULTS MAY KEEP A BODY AT ALL.
  //
  // The per-result cap bounds ONE payload; it does not bound how many are
  // preserved, and the pairing pass admits a result for every preserved
  // assistant turn. On a tool-heavy orchestrator that is most of the tail:
  // go-goal1 preserved 141 entries against a 100,000 budget and carried
  // ~164,000 -- roughly a hundred results each sitting near the 1,500 cap.
  // The cap had taken the floor from 442K to 200K by shrinking each payload
  // and could not take it further, because nothing counted the total.
  //
  // `maxTotalTokens` is that count. Results are admitted NEWEST FIRST: the
  // ones the model is most likely to still need keep their (capped) body,
  // and once the allowance is spent the older ones keep their message, role,
  // tool name and id but carry only the marker. Every message still
  // survives -- that was the point of capping rather than dropping -- and
  // the tail as a whole finally obeys the number it is measured against.
  //
  // 0 disables the total, which is the per-result-only behaviour.
  const admitted = new Set<number>();
  if (maxTotalTokens > 0) {
    let spent = 0;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if ((messages[i] as { role?: unknown }).role !== "toolResult") continue;
      const size = Math.min(estimateMessagesTokens([messages[i]]), maxTokens);
      // The newest result is always admitted. A tail whose non-result
      // messages have already spent the budget would otherwise return every
      // result as a bare marker, including the one that just came back.
      if (admitted.size === 0 || spent + size <= maxTotalTokens) {
        admitted.add(i);
        spent += size;
      }
    }
  }

  let cappedCount = 0;
  let tokensDropped = 0;
  let elidedCount = 0;
  let touched = false;
  const out: AgentMessage[] = [];

  for (let idx = 0; idx < messages.length; idx += 1) {
    const msg = messages[idx];
    if (!msg || typeof msg !== "object" || (msg as { role?: unknown }).role !== "toolResult") {
      out.push(msg);
      continue;
    }
    const before = estimateMessagesTokens([msg]);
    // Beyond the total allowance: keep the message, drop the body entirely.
    if (maxTotalTokens > 0 && !admitted.has(idx)) {
      const stub = elideWholly(msg, before);
      if (stub) {
        out.push(stub);
        elidedCount += 1;
        tokensDropped += before - estimateMessagesTokens([stub]);
        touched = true;
        continue;
      }
    }
    if (before <= maxTokens) {
      out.push(msg);
      continue;
    }

    const content = (msg as { content?: unknown }).content;
    if (!Array.isArray(content)) {
      // Not a shape we can trim safely. Keeping it whole is the honest
      // outcome: an unparsed result left at full size is a cost, a mangled
      // one is a correctness problem.
      out.push(msg);
      continue;
    }

    // Trim the TEXT blocks only, longest first, until the message fits.
    // Non-text blocks (images are already handled elsewhere) are left alone.
    const next = content.map((b) => (textOf(b) !== null ? { ...(b as object) } : b));
    const order = next
      .map((b, i) => ({ i, len: (textOf(b) ?? "").length }))
      .filter((e) => e.len > 0)
      .sort((a, b) => b.len - a.len);

    const headChars = Math.max(200, Math.floor(maxTokens * HEAD_SHARE * CHARS_PER_TOKEN));
    for (const { i } of order) {
      const current = estimateMessagesTokens([{ ...(msg as object), content: next } as AgentMessage]);
      if (current <= maxTokens) break;
      const text = textOf(next[i]);
      if (text === null || text.length <= headChars) continue;
      const dropped = Math.max(1, Math.floor((text.length - headChars) / CHARS_PER_TOKEN));
      (next[i] as { text: string }).text = text.slice(0, headChars) + elisionMarker(dropped);
    }

    const capped = { ...(msg as object), content: next } as AgentMessage;
    const after = estimateMessagesTokens([capped]);
    if (after < before) {
      cappedCount += 1;
      tokensDropped += before - after;
      touched = true;
      out.push(capped);
    } else {
      out.push(msg);
    }
  }

  return touched
    ? { messages: out, cappedCount, tokensDropped, elidedCount }
    : { messages, cappedCount: 0, tokensDropped: 0, elidedCount: 0 };
}


/** Replace a result's whole body with the marker, keeping the message.
 *
 *  For results past the total allowance. Returns null when the shape cannot
 *  be rewritten safely -- an unparsed result left whole is a cost; a mangled
 *  one is a correctness problem, the same trade the per-result cap makes. */
function elideWholly(msg: AgentMessage, beforeTokens: number): AgentMessage | null {
  const content = (msg as { content?: unknown }).content;
  if (!Array.isArray(content)) {
    return null;
  }
  let replaced = false;
  const next = content.map((b) => {
    if (textOf(b) === null) return b;
    replaced = true;
    return { ...(b as object), text: elisionMarker(beforeTokens).trimStart() };
  });
  if (!replaced) {
    return null;
  }
  return { ...(msg as object), content: next } as AgentMessage;
}
