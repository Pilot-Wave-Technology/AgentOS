/**
 * Tests for the enrichment path (findings 3.1/3.2 of
 * research/GEMINI_OPAQUE_AGENT_FAILURES_2026-08-16.md): recovering Gemini's real
 * finishReason and folding it into the raw error so `rawErrorHash` stops being
 * `sha256("An unknown error occurred")` for every failure.
 *
 * WHY THIS FILE EXISTS NOW RATHER THAN EARLIER: the live verification plan was to
 * trigger a real MALFORMED_FUNCTION_CALL against the local dev gateway and read the
 * enriched error off a JSON-logged run. Four batches (12 stages, 2026-08-18) produced
 * zero Gemini failures to observe — the failure rate is real but intermittent and not
 * reproducible on demand. Rather than leave this "probably fine", these tests exercise
 * the actual mechanism end-to-end: the same global-fetch interception, the same
 * AsyncLocalStorage run-scoping, and the same consume-on-read enrichment function the
 * gateway uses — with a scripted Gemini response standing in for the real API.
 */
import { afterAll, describe, expect, it } from "vitest";
import type { StreamFn } from "@mariozechner/pi-agent-core";
import {
  createGeminiFinishReasonLogger,
  enrichRawErrorWithGeminiFinishReason,
} from "./google-finish-reason-log.js";

// installInterceptor() captures whatever `globalThis.fetch` is AT FIRST INSTALL and
// closes over it forever (install-once, no swap/restore — see the module's header for
// why). So the mock has to be in place before the first createGeminiFinishReasonLogger
// call in this file, and stay swappable afterwards: a stable wrapper here delegates to
// a reassignable `currentFetchImpl`, which each test points at its own scripted response.
//
// Deliberately a PLAIN assignment, not `vi.stubGlobal` — this project's vitest.config.ts
// sets `unstubGlobals: true`, which calls `vi.unstubAllGlobals()` after every test and
// would revert `globalThis.fetch` to the real one. That would not just remove this
// stub; it would also silently discard the interceptor's OWN monkey-patch (installed
// on top of whatever `globalThis.fetch` was when the module first ran), since the
// interceptor's install-once design has no restore path of its own by design.
const realFetch = globalThis.fetch;
let currentFetchImpl: typeof fetch = () => {
  throw new Error("no fetch impl configured for this test");
};
globalThis.fetch = ((...args: Parameters<typeof fetch>) => currentFetchImpl(...args)) as typeof fetch;
afterAll(() => {
  globalThis.fetch = realFetch;
});

function chunkStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i++]));
      } else {
        controller.close();
      }
    },
  });
}

/** A scripted Gemini response: one candidate, given finishReason, no functionResponse
 * needed on the request side unless the test cares about the empty-turn suppression. */
function googleResponse(finishReason: string, opts?: { withText?: boolean }): Response {
  const candidate = opts?.withText
    ? { content: { parts: [{ text: "hello" }] }, finishReason }
    : { content: { role: "model" }, finishReason };
  const body = JSON.stringify({ candidates: [candidate] });
  return new Response(chunkStream([body]), { status: 200 });
}

/** Drain a response body and give the fire-and-forget background scan
 * (`void scanForAnomaly(...)` in the interceptor) a few ticks to finish reading its
 * own teed branch — it is not awaited by the interceptor by design (best-effort,
 * see the module header), so tests wait for it explicitly rather than relying on
 * the caller's own consumption of `passthrough`. */
async function drainAndSettle(response: Response): Promise<void> {
  if (response.body) {
    const reader = response.body.getReader();
    for (;;) {
      const { done } = await reader.read();
      if (done) break;
    }
  }
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setTimeout(r, 5));
  }
}

/** Run a streamFn body inside this run's AsyncLocalStorage scope, exactly as
 * attempt.ts does via `geminiFinishReasonLogger.wrapStreamFn(activeSession.agent.streamFn)`. */
async function runScoped(runId: string, body: () => Promise<Response>): Promise<Response> {
  const logger = createGeminiFinishReasonLogger({ runId, sessionId: `sess-${runId}` });
  let captured: Response | undefined;
  const stub: StreamFn = (async () => {
    captured = await body();
    return {} as never;
  }) as StreamFn;
  await logger.wrapStreamFn(stub)({} as never, {} as never, {});
  if (!captured) throw new Error("streamFn body never ran");
  return captured;
}

const GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent";

describe("Gemini finish-reason enrichment (end-to-end through the real fetch interceptor)", () => {
  it("records an abnormal finish reason and folds it into the raw error for that run", async () => {
    currentFetchImpl = async () => googleResponse("MALFORMED_FUNCTION_CALL");
    const response = await runScoped("run-a", () => fetch(GOOGLE_URL, { method: "POST" }));
    await drainAndSettle(response);

    const enriched = enrichRawErrorWithGeminiFinishReason("run-a", "An unknown error occurred");
    expect(enriched).toBe("An unknown error occurred [gemini finishReason=MALFORMED_FUNCTION_CALL]");
  });

  it("consumes the reason on read — a second enrichment for the same run gets the bare error", async () => {
    currentFetchImpl = async () => googleResponse("SAFETY");
    const response = await runScoped("run-consume", () => fetch(GOOGLE_URL, { method: "POST" }));
    await drainAndSettle(response);

    const first = enrichRawErrorWithGeminiFinishReason("run-consume", "An unknown error occurred");
    const second = enrichRawErrorWithGeminiFinishReason("run-consume", "An unknown error occurred");
    expect(first).toContain("finishReason=SAFETY");
    expect(second).toBe("An unknown error occurred"); // nothing left to attach — unchanged, not stale reuse
  });

  it("does not record a benign STOP finish reason — enrichment leaves the error untouched", async () => {
    currentFetchImpl = async () => googleResponse("STOP", { withText: true });
    const response = await runScoped("run-benign", () => fetch(GOOGLE_URL, { method: "POST" }));
    await drainAndSettle(response);

    const enriched = enrichRawErrorWithGeminiFinishReason("run-benign", "some other message");
    expect(enriched).toBe("some other message");
  });

  it("keeps two concurrent runs' reasons separate via AsyncLocalStorage (the 3.4 fix)", async () => {
    // Before 3.4 this shared a single module-level "current run" — under concurrency,
    // whichever run started last would win and mislabel the other's failure.
    let call = 0;
    currentFetchImpl = async () => {
      call += 1;
      return call === 1 ? googleResponse("MALFORMED_FUNCTION_CALL") : googleResponse("RECITATION");
    };

    const [responseA, responseB] = await Promise.all([
      runScoped("run-concurrent-a", () => fetch(GOOGLE_URL, { method: "POST" })),
      runScoped("run-concurrent-b", () => fetch(GOOGLE_URL, { method: "POST" })),
    ]);
    await Promise.all([drainAndSettle(responseA), drainAndSettle(responseB)]);

    const enrichedA = enrichRawErrorWithGeminiFinishReason("run-concurrent-a", "An unknown error occurred");
    const enrichedB = enrichRawErrorWithGeminiFinishReason("run-concurrent-b", "An unknown error occurred");
    const reasons = [enrichedA, enrichedB].join("|");
    expect(reasons).toContain("MALFORMED_FUNCTION_CALL");
    expect(reasons).toContain("RECITATION");
    expect(enrichedA).not.toBe(enrichedB);
  });

  it("returns the raw error unchanged with no runId or no rawError (fail-open)", () => {
    expect(enrichRawErrorWithGeminiFinishReason(undefined, "boom")).toBe("boom");
    expect(enrichRawErrorWithGeminiFinishReason("run-x", undefined)).toBeUndefined();
  });

  it("passes the underlying response through untouched (tee, not consume)", async () => {
    currentFetchImpl = async () => googleResponse("MALFORMED_FUNCTION_CALL");
    const response = await runScoped("run-passthrough", () => fetch(GOOGLE_URL, { method: "POST" }));
    const text = await response.text();
    expect(text).toContain("MALFORMED_FUNCTION_CALL"); // caller still gets the full real body
  });
});
