/**
 * MODEL REGISTRY — Gemini finish-reason diagnostics.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * OWNED BY THE MODEL REGISTRY FEATURE, NOT UPSTREAM OPENCLAW.
 * The whole implementation lives here; `run/attempt.ts` gets one call. A
 * rebase should only ever conflict on that call site.
 * ─────────────────────────────────────────────────────────────────────────
 *
 * WHY THIS EXISTS
 *
 * Gemini can end a turn in ~15 ways that are not `STOP` or `MAX_TOKENS`:
 * MALFORMED_FUNCTION_CALL, UNEXPECTED_TOOL_CALL, SAFETY, RECITATION,
 * PROHIBITED_CONTENT, BLOCKLIST, SPII, LANGUAGE, OTHER,
 * FINISH_REASON_UNSPECIFIED … The pinned `@mariozechner/pi-ai` collapses
 * every one of them into a single constant and throws away which it was:
 *
 *   providers/google.js:147-151  stopReason = mapStopReason(candidate.finishReason)
 *   providers/google-shared.js   ~15 reasons  ->  "error"
 *   providers/google.js:192-193  throw new Error("An unknown error occurred")
 *
 * The gateway then logs and hashes that constant. Its `rawErrorHash` is
 * therefore `sha256("An unknown error occurred")` = 0f56130f3d6a… for EVERY
 * such failure, regardless of cause — verified by hand. That is actively
 * misleading: it looks like a fingerprint identifying one deterministic bug
 * when it identifies nothing at all. Four parallel investigations of a real
 * production failure could not name the trigger, purely because this field
 * was destroyed before reaching any log.
 *
 * WHY IT HAS TO INTERCEPT fetch
 *
 * There is no other vantage point. `onPayload` (the hook the Anthropic
 * payload logger uses, anthropic-payload-log.ts) is REQUEST-side only for
 * Google — google.js:34 passes `params`, never the response. By the time any
 * stream event is emitted the reason has already been mapped away, so
 * wrapping `streamFn` cannot see it either, and pi-ai exposes no way to
 * supply a custom fetch to its `@google/genai` client (google.js:32
 * constructs it internally). Patching the dependency is not an option.
 *
 * So this installs one global `fetch` wrapper that acts ONLY on Google
 * Generative Language URLs, tees the response body, and scans the passing
 * bytes for the fields pi-ai is about to discard. Deliberately:
 *
 *   * install-once and never uninstalled — no swap/restore dance, so
 *     concurrent Gemini calls cannot race each other;
 *   * `body.tee()` rather than `clone()`, so the real consumer still gets a
 *     stream and nothing is buffered whole;
 *   * silent unless something is actually wrong — it logs only when the
 *     finish reason is neither STOP nor MAX_TOKENS, so the happy path costs
 *     one string compare per chunk and produces no noise;
 *   * fail-open everywhere. A diagnostic must never be able to break a real
 *     request, so every step is wrapped and errors are swallowed.
 *
 * WHEN TO DELETE THIS
 *
 * When pi-ai preserves the finish reason in the thrown error or the error
 * event (upstream fix), this becomes redundant. Until then it is the only
 * way to tell a safety block from a malformed tool call.
 */
import { AsyncLocalStorage } from "node:async_hooks";

import type { StreamFn } from "@mariozechner/pi-agent-core";

import { createSubsystemLogger } from "../logging/subsystem.js";

// Slash-separated to match the convention every other subsystem uses
// (agent/anthropic-payload, agents/auth-profiles …) — console output is gated
// per-subsystem by name, so an off-pattern name can silently not appear.
const log = createSubsystemLogger("agent/gemini-finish-reason");

const GOOGLE_HOST = "generativelanguage.googleapis.com";

/** The only two reasons that are not a failure in pi-ai's mapping. */
const BENIGN_FINISH_REASONS = new Set(["STOP", "MAX_TOKENS"]);

let installed = false;

/**
 * Markers that the response actually carried something back. Used to catch
 * SECOND failure mode: Gemini returns finishReason=STOP — benign as far as
 * pi-ai is concerned, so no error is raised anywhere — but with no text and no
 * function call. The turn then "succeeds" while doing nothing, which surfaces
 * far downstream as `No reply from agent.` and, for a council/security stage,
 * as "agent run ended without calling submit_review" with no explanation
 * attached. Observed live 2026-08-16 on the council `progress` stage.
 *
 * An empty STOP is always anomalous, so logging it cannot be noisy the way
 * logging "STOP without a function call" would be (that fires on every normal
 * chat turn).
 */
const CONTENT_MARKERS = ['"text"', '"functionCall"', '"inlineData"', '"executableCode"'];

/** Pull the fields pi-ai discards out of a raw response chunk. */
function extractDiagnostics(text: string): {
  finishReasons: string[];
  blockReason?: string;
  safetyish?: string;
} | null {
  const finishReasons = [...text.matchAll(/"finishReason"\s*:\s*"([A-Z_]+)"/g)].map((m) => m[1]);
  const blockReason = /"blockReason"\s*:\s*"([A-Z_]+)"/.exec(text)?.[1];
  if (!finishReasons.length && !blockReason) {
    return null;
  }
  // Only carry safety detail when it is likely to be the explanation; the
  // full ratings array is long and mostly NEGLIGIBLE noise.
  const safetyish = /"(?:blocked|probability)"\s*:\s*"?(?:true|HIGH|MEDIUM)"?/.test(text)
    ? text.slice(0, 1200)
    : undefined;
  return { finishReasons, blockReason, safetyish };
}

/**
 * Per-run context, scoped by AsyncLocalStorage rather than a module-level variable.
 *
 * WHY: the fetch interceptor is process-wide, so with several agents talking to Gemini
 * at once a single shared "current run" would label a log line with whichever run
 * happened to start last — silently mis-attributing a failure to an innocent stage.
 * The interceptor runs inside the streamFn's async context, so ALS carries the correct
 * run through automatically. (Same pattern already used by
 * src/plugins/runtime/gateway-request-scope.ts.)
 */
type GeminiLogContext = {
  runId?: string;
  sessionId?: string;
  provider?: string;
  modelId?: string;
};

const contextStore = new AsyncLocalStorage<GeminiLogContext>();

export type GeminiFinishReasonLogger = {
  /** Wrap the agent's streamFn so calls it makes carry this run's identity. */
  wrapStreamFn: (streamFn: StreamFn) => StreamFn;
};

/**
 * Create the logger for one run and ensure the interceptor is installed.
 * Mirrors createAnthropicPayloadLogger's shape so both hook in the same way.
 */
export function createGeminiFinishReasonLogger(
  context: GeminiLogContext,
): GeminiFinishReasonLogger {
  installInterceptor();
  const wrapStreamFn: GeminiFinishReasonLogger["wrapStreamFn"] = (streamFn) => {
    const wrapped: StreamFn = (model, ctx, options) =>
      contextStore.run({ ...context }, () => streamFn(model, ctx, options));
    return wrapped;
  };
  return { wrapStreamFn };
}

/** Install the process-wide fetch interceptor. Idempotent. */
function installInterceptor(): void {
  if (installed) {
    return;
  }
  const originalFetch = globalThis.fetch;
  if (typeof originalFetch !== "function") {
    return;
  }

  globalThis.fetch = async function patchedFetch(
    input: Parameters<typeof originalFetch>[0],
    init?: Parameters<typeof originalFetch>[1],
  ): ReturnType<typeof originalFetch> {
    const response = await originalFetch(input, init);
    try {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : ((input as Request)?.url ?? "");
      if (!url.includes(GOOGLE_HOST) || !response.body) {
        return response;
      }
      // An empty reply is NORMAL when the model is answering a tool result: the agent
      // has finished its work (e.g. submit_review returned {"ok":true}) and has nothing
      // left to say. Measured 2026-08-16: 109/109 empty turns were exactly this, so
      // flagging them produced pure noise. Detect the case from the REQUEST — if the
      // last content part is a functionResponse, an empty answer is expected.
      const lastWasToolResult = requestEndsWithToolResult(init?.body);
      const [passthrough, inspect] = response.body.tee();
      // Read the run identity from the enclosing async scope, not a shared global —
      // see GeminiLogContext. Empty when a Gemini call originates outside a wrapped
      // streamFn, which simply yields an unlabelled (still accurate) anomaly line.
      void scanForAnomaly(inspect, { ...(contextStore.getStore() ?? {}), url, lastWasToolResult });
      return new Response(passthrough, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    } catch {
      // Never let diagnostics break a real request.
      return response;
    }
  } as typeof originalFetch;

  installed = true;
  log.debug("installed — will report any Gemini finish reason other than STOP/MAX_TOKENS");
}

/**
 * True when the outgoing request's final turn is a tool result, i.e. the model is
 * being asked to respond to a functionResponse. An empty answer to that is the normal
 * way an agent signals "my work is done" and must not be reported as an anomaly.
 *
 * Body-shape tolerant and fail-open: anything unparseable returns false, which at worst
 * restores the old (noisy) behaviour rather than hiding a real anomaly.
 */
function requestEndsWithToolResult(body: unknown): boolean {
  try {
    if (typeof body !== "string" || !body.includes("functionResponse")) {
      return false;
    }
    const parsed = JSON.parse(body) as {
      contents?: Array<{ parts?: Array<Record<string, unknown>> }>;
    };
    const last = parsed.contents?.[parsed.contents.length - 1];
    return Boolean(last?.parts?.some((p) => "functionResponse" in p));
  } catch {
    return false;
  }
}

/**
 * Last abnormal finish reason seen per run, so the synchronous error path can recover
 * what pi-ai discarded.
 *
 * Bounded and insertion-ordered: a plain Map evicts its oldest entry past the cap, so a
 * long-lived gateway cannot accumulate these. Entries are consumed on read (see
 * enrichRawErrorWithGeminiFinishReason) to prevent an old reason being attached to a
 * later, unrelated error on the same run.
 */
const MAX_TRACKED_RUNS = 256;
const lastFinishReasonByRun = new Map<string, { reason: string; blockReason?: string }>();

function recordFinishReason(runId: string, reason: string, blockReason?: string): void {
  if (lastFinishReasonByRun.has(runId)) {
    lastFinishReasonByRun.delete(runId);
  } else if (lastFinishReasonByRun.size >= MAX_TRACKED_RUNS) {
    const oldest = lastFinishReasonByRun.keys().next().value;
    if (oldest !== undefined) {
      lastFinishReasonByRun.delete(oldest);
    }
  }
  lastFinishReasonByRun.set(runId, { reason, blockReason });
}

/**
 * Fold the recovered finish reason into the raw provider error.
 *
 * WHY THIS EXISTS. pi-ai throws one constant string for ~15 different Gemini finish
 * reasons, and the gateway hashes that string into `rawErrorHash`. The hash is therefore
 * `sha256` of a constant — identical for every such failure, discriminating nothing,
 * while *looking* like a fingerprint. It misled a full day of investigation on
 * 2026-08-16. Appending the real reason fixes both problems at once: the preview says
 * what happened, and the hash starts distinguishing a safety block from a malformed
 * tool call.
 *
 * BEST EFFORT, BY DESIGN. The reason is observed on a teed response stream while
 * `handleAgentEnd` is synchronous and cannot await it. Recording happens eagerly (the
 * moment the reason appears in a chunk), which closes most of the window, but a
 * particularly fast error path may still be enriched with nothing. That is acceptable:
 * an un-enriched error is exactly today's behaviour, so this can only add information,
 * never remove it. The authoritative record remains the `gemini_anomaly=` log line.
 */
export function enrichRawErrorWithGeminiFinishReason(
  runId: string | undefined,
  rawError: string | undefined,
): string | undefined {
  if (!runId || !rawError) {
    return rawError;
  }
  const found = lastFinishReasonByRun.get(runId);
  if (!found) {
    return rawError;
  }
  lastFinishReasonByRun.delete(runId);
  const block = found.blockReason ? ` blockReason=${found.blockReason}` : "";
  return `${rawError} [gemini finishReason=${found.reason}${block}]`;
}

async function scanForAnomaly(
  body: ReadableStream<Uint8Array>,
  ctx: {
    runId?: string;
    sessionId?: string;
    provider?: string;
    modelId?: string;
    url?: string;
    /** Model is answering a tool result — an empty reply is expected, not an anomaly. */
    lastWasToolResult?: boolean;
  },
): Promise<void> {
  try {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let tail = "";
    const reasons = new Set<string>();
    let blockReason: string | undefined;
    let safetyDetail: string | undefined;
    let sawContent = false;

    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      const chunk = decoder.decode(value, { stream: true });
      if (!sawContent && CONTENT_MARKERS.some((m) => chunk.includes(m))) {
        sawContent = true;
      }
      // Keep a rolling window so a field split across chunk boundaries is
      // still matched, without accumulating the whole (potentially huge) body.
      tail = (tail + chunk).slice(-8192);
      const found = extractDiagnostics(tail);
      if (found) {
        for (const r of found.finishReasons) {
          reasons.add(r);
          // Record EAGERLY, the moment the reason appears in a chunk, rather than
          // waiting for the stream to end. handleAgentEnd is synchronous and cannot
          // await this scan, so recording early is what makes the reason available in
          // time to enrich the error. See recordFinishReason.
          if (!BENIGN_FINISH_REASONS.has(r) && ctx.runId) {
            recordFinishReason(ctx.runId, r, found.blockReason);
          }
        }
        blockReason ??= found.blockReason;
        safetyDetail ??= found.safetyish;
      }
    }

    const anomalies = [...reasons].filter((r) => !BENIGN_FINISH_REASONS.has(r));
    // A "successful" turn that returned literally nothing — but ONLY worth reporting
    // when the model was NOT answering a tool result. See requestEndsWithToolResult:
    // an empty answer to a functionResponse is how an agent ends its work normally.
    const emptyOk =
      !anomalies.length &&
      !blockReason &&
      reasons.size > 0 &&
      !sawContent &&
      !ctx.lastWasToolResult;
    if (!anomalies.length && !blockReason && !emptyOk) {
      return;
    }
    const who =
      `provider=${ctx.provider ?? "?"} model=${ctx.modelId ?? "?"} ` +
      `runId=${ctx.runId ?? "?"} sessionId=${ctx.sessionId ?? "?"}`;
    // NOTE ON WORDING: these messages deliberately do NOT quote the downstream symptom
    // strings verbatim (the opaque pi-ai error text, or the runner's no-reply notice).
    // An earlier version did, which silently corrupted measurement: grepping the log for
    // those phrases counted this diagnostic's own output alongside the real occurrences,
    // inflating a 5-run batch's totals. Paraphrase only. Count `gemini_anomaly=` /
    // `gemini_empty_turn=`, which appear nowhere else.
    if (emptyOk) {
      log.warn(
        `gemini_empty_turn=${[...reasons].join(",")} — Gemini returned a turn with no text ` +
          `and no function call. pi-ai treats this as success, so nothing else reports it; ` +
          `downstream it looks like the agent simply never replied or never called its ` +
          `submit tool. ${who}`,
      );
      return;
    }
    log.warn(
      `gemini_anomaly=${anomalies.join(",") || "(none)"} — Gemini ended the turn on a finish ` +
        `reason pi-ai collapses into its generic unknown-error text, discarding this value. ` +
        `blockReason=${blockReason ?? "(none)"} ${who}` +
        (safetyDetail ? ` firstBytes=${JSON.stringify(safetyDetail)}` : ""),
    );
  } catch {
    // Diagnostics only.
  }
}
