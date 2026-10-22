import { describe, expect, it } from "vitest";

import type { AgentMessage } from "@mariozechner/pi-agent-core";

import { estimateMessagesTokens } from "../compaction.js";
import { capPreservedToolResults } from "./preserved-tool-result-cap.js";
import { splitPreservedRecentTurnsByTokens } from "./compaction-safeguard.js";

/**
 * The budget does not bound the preserved tail.
 *
 * `splitPreservedRecentTurnsByTokens` counts what its backward walk adds,
 * and then a second pass adds every `toolResult` paired with a preserved
 * assistant turn WITHOUT counting it. Those are preserved, uncounted and
 * unbudgeted. Measured on a goal orchestrator: the safeguard logged
 * `budget=100000 preserved=181 (~99141 tokens)` and the next call carried
 * 212,303 against a ~29K fixed prefix.
 *
 * Capping the RESULT keeps every message and shortens only the payload.
 */

const tr = (id: string, text: string): AgentMessage =>
  ({
    role: "toolResult",
    toolName: "read",
    toolCallId: id,
    content: [{ type: "text", text }],
  }) as unknown as AgentMessage;

const assistantCalling = (id: string): AgentMessage =>
  ({
    role: "assistant",
    content: [{ type: "toolCall", id, name: "read", arguments: {} }],
  }) as unknown as AgentMessage;

const user = (text: string): AgentMessage =>
  ({ role: "user", content: [{ type: "text", text }] }) as unknown as AgentMessage;

const big = (n: number) => "x".repeat(n);

describe("capPreservedToolResults", () => {
  it("is a no-op when disabled, so an unset deployment is unchanged", () => {
    const msgs = [tr("a", big(80_000))];
    for (const off of [0, -1, Number.NaN]) {
      const out = capPreservedToolResults(msgs, off);
      expect(out.messages).toBe(msgs); // same reference: nothing copied
      expect(out.cappedCount).toBe(0);
    }
  });

  it("shortens an oversized result and keeps the message", () => {
    const msgs = [user("go"), tr("a", big(60_000)), assistantCalling("a")];
    const out = capPreservedToolResults(msgs, 500);

    expect(out.messages).toHaveLength(3); // nothing dropped
    expect(out.cappedCount).toBe(1);
    expect(out.tokensDropped).toBeGreaterThan(0);
    expect(estimateMessagesTokens([out.messages[1]])).toBeLessThan(
      estimateMessagesTokens([msgs[1]]),
    );
  });

  it("never empties a result — an empty tool_result is an API error", () => {
    const out = capPreservedToolResults([tr("a", big(60_000))], 50);
    const content = (out.messages[0] as unknown as { content: { text?: string }[] }).content;
    expect(content).toHaveLength(1);
    expect((content[0].text ?? "").length).toBeGreaterThan(0);
  });

  it("says in the transcript what it removed", () => {
    const out = capPreservedToolResults([tr("a", big(60_000))], 500);
    const text = (out.messages[0] as unknown as { content: { text: string }[] }).content[0].text;
    expect(text).toMatch(/tokens of tool output elided at compaction/);
    expect(text).toMatch(/re-run the tool/);
  });

  it("keeps the role, the tool name and the id, so pairing survives", () => {
    const out = capPreservedToolResults([tr("call-7", big(60_000))], 500);
    const m = out.messages[0] as unknown as Record<string, unknown>;
    expect(m.role).toBe("toolResult");
    expect(m.toolName).toBe("read");
    expect(m.toolCallId).toBe("call-7");
  });

  it("leaves small results and non-toolResult messages alone", () => {
    const msgs = [user("hello"), tr("a", "short"), assistantCalling("a")];
    const out = capPreservedToolResults(msgs, 5_000);
    expect(out.messages).toBe(msgs);
    expect(out.cappedCount).toBe(0);
  });

  it("does not mutate its input", () => {
    const original = big(60_000);
    const msgs = [tr("a", original)];
    capPreservedToolResults(msgs, 500);
    const text = (msgs[0] as unknown as { content: { text: string }[] }).content[0].text;
    expect(text).toBe(original);
  });
});

describe("splitPreservedRecentTurnsByTokens with a cap", () => {
  // The real shape: an assistant turn whose paired tool result is enormous.
  // The pairing pass preserves that result whatever the budget says.
  const conversation = (): AgentMessage[] => [
    user("old"),
    assistantCalling("old-1"),
    tr("old-1", big(40_000)),
    user("recent"),
    assistantCalling("recent-1"),
    tr("recent-1", big(120_000)),
  ];

  it("preserves the same MESSAGES with and without the cap", () => {
    const uncapped = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
    });
    const capped = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
      preservedToolResultMaxTokens: 500,
    });
    expect(capped.preservedMessages).toHaveLength(uncapped.preservedMessages.length);
    expect(capped.preservedMessages.map((m) => (m as { role: string }).role)).toEqual(
      uncapped.preservedMessages.map((m) => (m as { role: string }).role),
    );
  });

  it("but carries far fewer tokens", () => {
    const uncapped = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
    });
    const capped = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
      preservedToolResultMaxTokens: 500,
    });
    const before = estimateMessagesTokens(uncapped.preservedMessages);
    const after = estimateMessagesTokens(capped.preservedMessages);
    expect(before).toBeGreaterThan(10_000); // the unbudgeted tail, as observed
    expect(after).toBeLessThan(before / 2);
  });

  it("leaves the summarizable half untouched", () => {
    const uncapped = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
    });
    const capped = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
      preservedToolResultMaxTokens: 500,
    });
    // What gets summarized is chosen before any capping, so the summary the
    // model writes is of exactly the same history either way.
    expect(capped.summarizableMessages).toEqual(uncapped.summarizableMessages);
  });

  it("without the setting, behaves exactly as before", () => {
    const withNothing = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
    });
    const withZero = splitPreservedRecentTurnsByTokens({
      messages: conversation(),
      keepRecentTokensBudget: 2_000,
      preservedToolResultMaxTokens: 0,
    });
    expect(withZero.preservedMessages).toEqual(withNothing.preservedMessages);
  });
});

/**
 * THE TOTAL, not just each one.
 *
 * The per-result cap bounds ONE payload and nothing bounds how many are
 * preserved — the pairing pass admits a result for every preserved assistant
 * turn. Measured on go-goal1 (2026-10-06, next): 141 preserved entries
 * carrying ~164,000 tokens against a 100,000 budget, which is roughly a
 * hundred results each sitting near the 1,500 cap. The cap had taken the
 * floor from 442K to 200K and could go no further on its own.
 */
describe("capPreservedToolResults with a total allowance", () => {
  const manyResults = (n: number, size: number): AgentMessage[] => {
    const out: AgentMessage[] = [user("go")];
    for (let i = 0; i < n; i += 1) {
      out.push(assistantCalling(`c${i}`), tr(`c${i}`, big(size)));
    }
    return out;
  };

  it("bounds the SUM, not only each result", () => {
    const msgs = manyResults(20, 60_000);
    const perResultOnly = capPreservedToolResults(msgs, 1_500);
    const withTotal = capPreservedToolResults(msgs, 1_500, 6_000);

    const a = estimateMessagesTokens(perResultOnly.messages);
    const b = estimateMessagesTokens(withTotal.messages);
    assertNotEqual(a, b);
    if (!(b < a)) throw new Error(`total allowance did not reduce: ${b} >= ${a}`);
    if (!(b < 20_000)) throw new Error(`20 capped results still carry ${b} tokens`);
  });

  it("keeps every message — nothing is dropped, only emptied", () => {
    const msgs = manyResults(20, 60_000);
    const out = capPreservedToolResults(msgs, 1_500, 3_000);
    expect(out.messages).toHaveLength(msgs.length);
    expect(out.elidedCount).toBeGreaterThan(0);
  });

  it("admits NEWEST first — the oldest results are the ones emptied", () => {
    const msgs = manyResults(6, 60_000);
    const out = capPreservedToolResults(msgs, 1_500, 3_000);
    const bodies = out.messages
      .filter((m) => (m as { role: string }).role === "toolResult")
      .map((m) => estimateMessagesTokens([m]));
    const oldest = bodies[0];
    const newest = bodies[bodies.length - 1];
    if (!(newest > oldest)) {
      throw new Error(
        `oldest=${oldest} newest=${newest}: the newest result should keep its `
        + "body, the oldest should be the marker",
      );
    }
  });

  it("always admits the newest result, even with no allowance left", () => {
    // A tail whose own messages have spent the budget would otherwise hand
    // back every result as a bare marker, including the one that just
    // returned — the single result the model most certainly needs.
    const msgs = manyResults(5, 60_000);
    const out = capPreservedToolResults(msgs, 1_500, 0 + 1);
    const results = out.messages.filter(
      (m) => (m as { role: string }).role === "toolResult",
    );
    const newest = estimateMessagesTokens([results[results.length - 1]]);
    if (!(newest > 200)) {
      throw new Error(`the newest result was emptied too (${newest} tokens)`);
    }
  });

  it("an emptied result still says what went, and is never empty", () => {
    const out = capPreservedToolResults(manyResults(4, 60_000), 1_500, 2_000);
    for (const m of out.messages) {
      if ((m as { role: string }).role !== "toolResult") continue;
      const c = (m as unknown as { content: { text?: string }[] }).content;
      expect(c).toHaveLength(1);
      expect((c[0].text ?? "").length).toBeGreaterThan(0);
      expect(c[0].text).toMatch(/tokens of tool output elided at compaction/);
    }
  });

  it("0 means no total — the per-result-only behaviour is unchanged", () => {
    const msgs = manyResults(8, 60_000);
    const withZero = capPreservedToolResults(msgs, 1_500, 0);
    const without = capPreservedToolResults(msgs, 1_500);
    expect(estimateMessagesTokens(withZero.messages))
      .toBe(estimateMessagesTokens(without.messages));
    expect(withZero.elidedCount).toBe(0);
  });
});

function assertNotEqual(a: number, b: number): void {
  if (a === b) throw new Error(`expected a difference, both were ${a}`);
}

/**
 * A tool loop in reply to ONE human message can never be summarized by the
 * pair anchor: the walk keeps going until it meets that user message, so
 * every call and result in the loop -- including a huge one -- is "recent".
 * Async compaction triggered, found nothing to summarize and skipped on
 * every later turn while the session sat above its threshold. Its last
 * resort anchors on the latest assistant step instead.
 */
describe("splitPreservedRecentTurnsByTokens anchor", () => {
  const loop = [
    user("summarize the repo"),
    assistantCalling("a"),
    tr("a", big(200_000)),
    assistantCalling("b"),
    tr("b", "ok"),
    assistantCalling("c"),
    tr("c", "ok"),
  ];

  it("keeps a whole single-message tool loop with the default pair anchor", () => {
    const out = splitPreservedRecentTurnsByTokens({
      messages: loop,
      keepRecentTokensBudget: 20_000,
    });
    expect(out.summarizableMessages).toHaveLength(0);
  });

  it("opens the loop when anchored on the latest assistant step", () => {
    const out = splitPreservedRecentTurnsByTokens({
      messages: loop,
      keepRecentTokensBudget: 1,
      anchorAssistantOnly: true,
    });
    // The latest step and its result stay; the request, the huge result and
    // the earlier steps go to the summarizer.
    expect(out.preservedMessages).toEqual([loop[5], loop[6]]);
    expect(out.summarizableMessages).toContain(loop[0]);
    expect(out.summarizableMessages).toContain(loop[2]);
  });

  it("leaves the default anchor unchanged for the in-line safeguard", () => {
    const withoutFlag = splitPreservedRecentTurnsByTokens({
      messages: loop,
      keepRecentTokensBudget: 1,
    });
    expect(withoutFlag.summarizableMessages).toHaveLength(0);
  });
});
