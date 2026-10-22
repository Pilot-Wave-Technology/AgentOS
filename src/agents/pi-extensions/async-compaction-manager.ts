/**
 * Async Compaction Manager — fires LLM-summary compaction in the
 * background so user turns never block on the multi-minute summarizer
 * call.
 *
 * Wired end-to-end:
 *  - extensions.ts (asyncEnabled=true disables the sync safeguard so
 *    only async manager owns compaction trigger; logs at startup)
 *  - pi-embedded-subscribe.ts (onTurnComplete callback fires after
 *    each recordAssistantUsage; passes per-turn usage to manager)
 *  - attempt.ts (subscribeEmbeddedPiSession is invoked with an
 *    onTurnComplete that closes over cfg/sessionManager/model/apiKey
 *    and calls maybeRequestAsyncCompaction)
 *
 * Activate by setting `agents.defaults.compaction.asyncEnabled: true`
 * in openclaw.json. Logs prefix on the VM:
 *   grep '[async-compaction]' /var/log/openclaw-18789.log
 *
 * State machine per session:
 *
 *   IDLE → SCHEDULED → SNAPSHOTTING → SUMMARIZING → SPLICING → IDLE
 *     ▲                                                          │
 *     └──────────────────────────────────────────────────────────┘
 *
 * Triggered when post-turn token count crosses
 * `agents.defaults.compaction.asyncSoftTriggerPct × contextWindow`.
 *
 * When `asyncEnabled` is true, pi's built-in safeguard auto-compaction
 * is DISABLED in extensions.ts so we own the trigger entirely. The
 * splice uses `sessionManager.appendCompaction()` — the same primitive
 * the safeguard would have used — so pi's prompt-build sees the
 * summary on the next turn.
 *
 * Logs prefix: "[async-compaction]" — grep for this on the VM to trace
 * the state machine for any session.
 */

import type { AgentMessage } from "@mariozechner/pi-agent-core";
import type { Api, Model } from "@mariozechner/pi-ai";
import { createSubsystemLogger } from "../../logging/subsystem.js";
import type { OpenClawConfig } from "../../config/config.js";
import { updateSessionStoreEntry } from "../../config/sessions.js";
import type { CompactionSummarizationInstructions } from "../compaction.js";
import { estimateMessagesTokens, summarizeInStages } from "../compaction.js";
import { splitPreservedRecentTurnsByTokens } from "./compaction-safeguard.js";
import { emitAgentEvent } from "../../infra/agent-events.js";

const log = createSubsystemLogger("async-compaction");

/**
 * Per-session memory-flush callback registry. The auto-reply layer
 * (agent-runner.ts) registers a flush executor for each session; the
 * compaction pipeline invokes it flush-first, before summarizing, so
 * MEMORY.md captures the full pre-compaction conversation. This decouples
 * the flush (auto-reply layer) from the compaction job (this module)
 * without a hard cross-layer import — the manager only holds an opaque
 * `() => Promise<void>`. Single trigger: flush + summarize + splice all
 * happen inside one compaction job, so there is no separate flush gate.
 */
const compactionFlushRegistry = new Map<string, () => Promise<void>>();
export function registerCompactionFlush(
  sessionKey: string,
  flush: () => Promise<void>,
): void {
  if (!sessionKey) {
    return;
  }
  compactionFlushRegistry.set(sessionKey, flush);
}

/**
 * Per-session STANDALONE memory-consolidation callback registry. Separate from
 * the compaction flush above: this fires per-turn (not per-compaction) to keep
 * MEMORY.md under cap independent of whether a compaction has happened. The
 * callback is the auto-reply layer's consolidation-only executor (registered in
 * agent-runner.ts); it internally checks the MEMORY.md size trigger + the
 * memoryConsolidationAt cooldown, so this module only needs an in-flight guard
 * (so a turn completing during the multi-second consolidation turn doesn't
 * stack a second one) and an overlap guard against an in-progress compaction
 * (which runs its own flush/consolidation flush-first).
 */
const standaloneConsolidationRegistry = new Map<string, () => Promise<void>>();
export function registerStandaloneConsolidation(
  sessionKey: string,
  consolidate: () => Promise<void>,
): void {
  if (!sessionKey) {
    return;
  }
  standaloneConsolidationRegistry.set(sessionKey, consolidate);
}

const standaloneConsolidationInFlight = new Set<string>();

/**
 * Fire-and-forget per-turn standalone consolidation check. Cheap to call after
 * every turn: returns immediately unless a consolidation callback is registered
 * for the session, one isn't already running, and no async compaction is
 * mid-flight for the same session. The registered callback owns the actual
 * size + cooldown gating, so a no-op (file under cap / on cooldown) is a fast
 * return inside it.
 */
export function maybeRunStandaloneConsolidation(params: { sessionKey: string }): void {
  const sessionKey = params.sessionKey;
  if (!sessionKey) {
    return;
  }
  const consolidate = standaloneConsolidationRegistry.get(sessionKey);
  if (!consolidate) {
    return;
  }
  if (standaloneConsolidationInFlight.has(sessionKey)) {
    return;
  }
  // Don't run while a compaction is in progress for this session — that job
  // does its own flush-first (which may itself consolidate); overlapping
  // MEMORY.md rewrites would race.
  const compState = registry.get(sessionKey);
  if (compState && compState.phase !== "idle") {
    return;
  }
  standaloneConsolidationInFlight.add(sessionKey);
  void consolidate()
    .catch((err) => {
      log.warn(
        `[standalone-consolidation] failed: session=${sessionKey} err=${String(err)}`,
      );
    })
    .finally(() => {
      standaloneConsolidationInFlight.delete(sessionKey);
    });
}

// ── Types ──────────────────────────────────────────────────────────────────

/** Phase of the per-session state machine.
 *  - idle/scheduled/snapshotting/summarizing/splicing: normal lifecycle
 *  - failed: transient — auto-reset to idle after a 30s cooldown
 *  - disabled: back-off after the failed-attempt count hits
 *    asyncMaxFailedAttempts. SELF-HEALING — auto-re-enables (→ idle) after
 *    DISABLED_COOLDOWN_MS (half-open). Does NOT gate agent turns; it only
 *    pauses RE-TRIGGERING of background compaction during the back-off. */
export type AsyncCompactionPhase =
  | "idle"
  | "scheduled"
  | "snapshotting"
  | "summarizing"
  | "splicing"
  | "failed"
  | "disabled";

/** Minimal interface for the pi session manager methods we depend on.
 *  Only `appendCompaction` is touched on the SessionManager itself.
 *  Reads go through the `getEntries` callback passed by the caller —
 *  see below. */
export interface AsyncCompactionSessionManager {
  appendCompaction(
    summary: string,
    firstKeptEntryId: string,
    tokensBefore: number,
    details: Record<string, unknown> | undefined,
    fromHook: boolean,
  ): void;
}

/** Shape of a single session entry as exposed by pi's
 *  `SessionManager.getEntries()`. Pi stores a list of envelopes:
 *  conversation messages, compaction summaries, model changes, etc.
 *  We only consume `type`, `id`, and the nested `message` / `summary`
 *  fields — every other field is opaque to us. The 2026-05-26
 *  incident proved the hard way that pi's session.messages is the
 *  UNWRAPPED bodies (no id field), while getEntries() preserves the
 *  envelope structure with ids — which is what `appendCompaction`
 *  needs as `firstKeptEntryId`. */
export type AsyncCompactionSessionEntry = {
  type: string;
  id?: string;
  message?: AgentMessage;
  summary?: string;
};

/** Per-session state. */
type AsyncCompactionState = {
  phase: AsyncCompactionPhase;
  sessionKey: string;
  startedAt: number;
  abortController?: AbortController;
  lastError?: string;
  // Cumulative counters for telemetry
  fired: number;
  succeeded: number;
  failed: number;
  // Stable synthetic runId for this compaction round — used so the
  // start/end agent_event frames share a runId (and a seq counter)
  // that's distinct from the triggering turn's run (which is already
  // complete by the time async compaction starts).
  roundRunId?: string;
};

/** Emit a `stream: "compaction"` agent_event for this round. The control
 *  panel's _AgentSession listens for these to gate dispatch through the
 *  chat_module queue (defer new turns while compaction is in flight). */
function emitAsyncCompactionEvent(
  state: AsyncCompactionState,
  phase: "start" | "end",
  extra: Record<string, unknown> = {},
): void {
  if (!state.roundRunId) {
    return;
  }
  emitAgentEvent({
    runId: state.roundRunId,
    stream: "compaction",
    sessionKey: state.sessionKey,
    data: {
      phase,
      source: "async",
      sessionKey: state.sessionKey,
      ...extra,
    },
  });
}

/** Snapshot captured at trigger time. */
type Snapshot = {
  capturedAt: number;
  capturedTokens: number;
  // Shallow copy of the message ARRAY. Messages themselves are immutable
  // post-append in pi's model, so this is safe.
  messages: AgentMessage[];
};

/** Result returned by the background summarizer. */
type SummaryResult = {
  summary: string;
  firstKeptEntryId: string;
  tokensBefore: number;
  /**
   * Estimated context size AFTER the splice = tokensBefore minus the
   * summarized portion plus the summary. Written to the session entry's
   * totalTokens in the post-splice store update so the memory-flush gate
   * reads the reduced size immediately — otherwise the next reply cycle
   * reads the STALE pre-splice totalTokens (the gate runs before the next
   * turn refreshes it) and spurious-flushes right after every compaction.
   */
  tokensAfter: number;
  summarizationMs: number;
};

// ── State registry (per session key) ──────────────────────────────────────
//
// Keyed by sessionKey (string) rather than sessionManager (object), because
// pi opens a fresh SessionManager on every attempt — a WeakMap key would
// be GC'd between turns and the DISABLED state we set after N failures
// would silently reset. With a string key, the state survives across
// turns within the gateway process's lifetime; gateway restart is the
// operator unblock mechanism.

const registry = new Map<string, AsyncCompactionState>();

// Global semaphore — cap concurrent summarizer calls across all sessions
let inFlightSummarizers = 0;

// Hard deadline for one background summarizer round. If the summarizer hangs
// (provider stall, dropped socket), abort it so the phase can't wedge at
// "summarizing" forever and permanently leak an inFlightSummarizers slot (which
// would silently starve compaction for every session). Real rounds run 30s–4min.
const SUMMARIZER_TIMEOUT_MS = 300_000;
// After asyncMaxFailedAttempts consecutive failures, stop RE-TRIGGERING for this
// long, then re-enable (half-open). This is NOT a turn gate — the agent keeps
// working throughout; it only paces retries of the background summarizer.
const DISABLED_COOLDOWN_MS = 600_000;

function getOrInitState(sessionKey: string): AsyncCompactionState {
  let state = registry.get(sessionKey);
  if (!state) {
    state = {
      phase: "idle",
      sessionKey,
      startedAt: 0,
      fired: 0,
      succeeded: 0,
      failed: 0,
    };
    registry.set(sessionKey, state);
  }
  return state;
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Inspect the cumulative token count after a turn and, if above the
 * configured async soft trigger, fire a background compaction.
 *
 * Cheap to call after every turn — returns immediately if state machine
 * is already past IDLE or if the trigger threshold isn't met.
 */
export function maybeRequestAsyncCompaction(params: {
  cfg?: OpenClawConfig;
  sessionManager: AsyncCompactionSessionManager;
  /** Returns the current envelope list via pi's
   *  `sessionManager.getEntries()`. We need the envelopes (not the
   *  unwrapped `.messages`) because `appendCompaction` requires the
   *  envelope id of the first preserved entry as `firstKeptEntryId`. */
  getEntries: () => AsyncCompactionSessionEntry[] | undefined;
  sessionKey: string;
  totalTokens: number;
  contextWindow: number;
  model: Model<Api> | undefined;
  apiKey: string | undefined;
  /** Per-agent summary prompt — the prompt template admin pushes via
   *  `agents.list[i].compaction.summaryPrompt` (typically the
   *  migration-069 derived structured prompt). Without this, the
   *  summarizer falls back to a minimal default and produces terse
   *  ~500-char output instead of the ~10K-15K structured summaries
   *  the sync safeguard yields. */
  customInstructions?: string;
  /** Identifier preservation policy (strict/off/custom) and the custom
   *  text if applicable. Passed through to summarizeInStages so the
   *  summarizer keeps UUIDs, file paths, port numbers, etc. exact. */
  summarizationInstructions?: CompactionSummarizationInstructions;
  /** Reserve tokens floor for the summarizer's own LLM call. */
  reserveTokens?: number;
  /** Token budget for recent-turn preservation (mirror of safeguard
   *  keepRecentTokensBudget — derived from keepRecentTokensPct × contextWindow). */
  keepRecentTokensBudget?: number;

  /** Cap on a single preserved `toolResult`, in estimated tokens. The
   *  async path needs this as much as the sync one: both call the same
   *  splitter, and the pairing pass there preserves tool results the
   *  budget never counted. */
  preservedToolResultMaxTokens?: number;
  /** Per-agent sessions.json path. When provided, the manager bumps
   *  compactionCount AND resets memoryFlushLastFiredAtTokens=null on
   *  every successful SPLICED. Without this, async splices don't tick
   *  the disk-side counter (pi's appendCompaction doesn't fire
   *  auto_compaction_end through the path that triggers
   *  incrementRunCompactionCount), so the memory-flush cycle never
   *  resets and auto-flush stays permanently blocked. Verified
   *  2026-05-26 on go-amz19: SPLICED fired clean but compactionCount
   *  stayed at 0 and a stale memoryFlushLastFiredAtTokens=41327 kept
   *  blocking new flushes. */
  storePath?: string;
}): void {
  const cfg = params.cfg?.agents?.defaults?.compaction;
  if (!cfg?.asyncEnabled) {
    return;
  }
  if (!params.model || !params.apiKey) {
    return;
  }

  const triggerPct = cfg.asyncSoftTriggerPct ?? 0.8;
  if (typeof triggerPct !== "number" || triggerPct <= 0 || triggerPct > 1) {
    return;
  }
  const threshold = Math.floor(params.contextWindow * triggerPct);
  if (params.totalTokens < threshold) {
    return;
  }

  const state = getOrInitState(params.sessionKey);
  // FAILED state used to allow immediate re-trigger, which caused a
  // "flap" loop on 2026-05-26: every post-trigger turn re-fired the
  // pipeline while the cooldown timer was still pending. Now only
  // IDLE allows re-trigger — the 30s setTimeout that flips FAILED →
  // IDLE is the single source of truth for retry cadence.
  if (state.phase !== "idle") {
    if (state.phase === "disabled") {
      // Backing off after the consecutive-failure cap. This does NOT gate the
      // agent's turns (attempt.ts no longer rejects) — it only skips RE-
      // TRIGGERING compaction until the cooldown re-enables us (half-open).
      // Log for visibility on each trigger dropped during the back-off.
      log.warn(
        `[async-compaction] skip (backing off): session=${params.sessionKey} consecutiveFailures=${state.failed} lastError=${state.lastError ?? "?"}`,
      );
    } else {
      log.debug(
        `[async-compaction] skip: session=${params.sessionKey} already in phase=${state.phase} (fired=${state.fired})`,
      );
    }
    return;
  }

  const maxConcurrent = cfg.asyncMaxConcurrent ?? 4;
  if (inFlightSummarizers >= maxConcurrent) {
    log.warn(
      `[async-compaction] cap reached: session=${params.sessionKey} inFlight=${inFlightSummarizers} cap=${maxConcurrent} — deferring trigger`,
    );
    return;
  }

  state.phase = "scheduled";
  state.startedAt = Date.now();
  state.roundRunId = `cmpct-async-${state.sessionKey}-${state.startedAt}`;
  state.fired += 1;
  state.lastError = undefined;
  log.info(
    `[async-compaction] triggered: session=${params.sessionKey} totalTokens=${params.totalTokens} threshold=${threshold} (${Math.round(
      (params.totalTokens / params.contextWindow) * 100,
    )}% of ${params.contextWindow}) fired#${state.fired}`,
  );
  // Tell subscribers (control panel chat dispatcher) the session is now
  // mid-compaction so it can defer new turns to this sessionKey until the
  // matching `phase: "end"` event fires below.
  emitAsyncCompactionEvent(state, "start", {
    totalTokens: params.totalTokens,
    threshold,
    fired: state.fired,
  });

  // Fire-and-forget. Defensive .catch on the outer promise so any
  // future bug inside the pipeline cannot become an unhandled rejection
  // and take down the gateway process (the 2026-05-26 sessionManager.messages
  // incident did exactly that — node treats unhandled rejections as
  // process-fatal in newer versions).
  runAsyncCompactionPipeline({
    state,
    cfg: params.cfg!,
    sessionManager: params.sessionManager,
    getEntries: params.getEntries,
    model: params.model,
    apiKey: params.apiKey,
    customInstructions: params.customInstructions,
    summarizationInstructions: params.summarizationInstructions,
    reserveTokens: params.reserveTokens ?? 12000,
    contextWindow: params.contextWindow,
    totalTokens: params.totalTokens,
    keepRecentTokensBudget: params.keepRecentTokensBudget ?? 0,
    preservedToolResultMaxTokens: params.preservedToolResultMaxTokens ?? 0,
    storePath: params.storePath,
  }).catch((err) => {
    handlePipelineFailure({
      state,
      errorMessage: `pipeline CRASHED (caught): ${err instanceof Error ? err.message : String(err)}`,
      maxFailedAttempts: cfg.asyncMaxFailedAttempts ?? 2,
      context: "pipeline-outer-catch",
    });
  });
}

/**
 * Returns the current state snapshot for a session (or null). Exposed for
 * observability/diagnostics — it does NOT gate turns. (Turns are never gated
 * on async-compaction state; async compaction is a background optimization.)
 */
export function getAsyncCompactionState(
  sessionKey: string,
): AsyncCompactionState | null {
  return registry.get(sessionKey) ?? null;
}

/**
 * Observability predicate: returns { disabled: true, ... } iff the session's
 * async compaction is currently in its self-healing back-off. Diagnostics
 * only — no caller uses this to block a turn.
 */
export function isAsyncCompactionDisabled(sessionKey: string): {
  disabled: boolean;
  consecutiveFailures: number;
  lastError?: string;
} {
  const state = registry.get(sessionKey);
  if (!state || state.phase !== "disabled") {
    return { disabled: false, consecutiveFailures: state?.failed ?? 0 };
  }
  return {
    disabled: true,
    consecutiveFailures: state.failed,
    lastError: state.lastError,
  };
}

/**
 * Operator unblock — clear the in-memory state for a session so the
 * next trigger can attempt again. Currently invoked only on gateway
 * restart (implicit via registry being process-local); future
 * admin RPC would call this directly.
 */
export function clearAsyncCompactionState(sessionKey: string): void {
  registry.delete(sessionKey);
}

// ── Pipeline (background) ──────────────────────────────────────────────────

async function runAsyncCompactionPipeline(params: {
  state: AsyncCompactionState;
  cfg: OpenClawConfig;
  sessionManager: AsyncCompactionSessionManager;
  getEntries: () => AsyncCompactionSessionEntry[] | undefined;
  model: Model<Api>;
  apiKey: string;
  customInstructions?: string;
  summarizationInstructions?: CompactionSummarizationInstructions;
  reserveTokens: number;
  contextWindow: number;
  totalTokens: number;
  keepRecentTokensBudget: number;
  preservedToolResultMaxTokens: number;
  storePath?: string;
}): Promise<void> {
  const { state } = params;
  const cfg = params.cfg.agents?.defaults?.compaction;
  const debounceMs = cfg?.asyncDebounceMs ?? 5000;

  // Debounce — let any in-flight turn finish before snapshotting.
  await sleep(debounceMs);

  // ── FLUSH-FIRST ──────────────────────────────────────────────────────
  // Distill MEMORY.md from the full pre-compaction conversation before we
  // snapshot/summarize/prune it. Registered by the auto-reply layer. This
  // is the ONLY place the memory flush fires now (single trigger, once per
  // compaction cycle) — best-effort, a flush failure must never abort the
  // compaction.
  const flushFn = compactionFlushRegistry.get(state.sessionKey);
  if (flushFn) {
    try {
      await flushFn();
    } catch (err) {
      log.warn(
        `[async-compaction] flush-before-compact failed (continuing): session=${state.sessionKey} err=${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  // ── SNAPSHOTTING ─────────────────────────────────────────────────────
  state.phase = "snapshotting";
  const liveEntries = params.getEntries();
  if (!liveEntries || !Array.isArray(liveEntries)) {
    log.warn(
      `[async-compaction] snapshot SKIP: session=${state.sessionKey} getEntries() returned non-array (${typeof liveEntries}) — aborting`,
    );
    state.phase = "idle";
    emitAsyncCompactionEvent(state, "end", { ok: false, reason: "snapshot-invalid" });
    return;
  }
  const entriesSnapshot = [...liveEntries];

  // Carry-forward of the prior compaction summary: scan envelopes
  // backward for type==="compaction". This is the envelope-level
  // equivalent of pi's event.preparation.previousSummary that the
  // sync safeguard receives.
  let priorSummaryIdx = -1;
  for (let i = entriesSnapshot.length - 1; i >= 0; i -= 1) {
    if (entriesSnapshot[i]?.type === "compaction") {
      priorSummaryIdx = i;
      break;
    }
  }
  const previousSummary =
    priorSummaryIdx >= 0 ? entriesSnapshot[priorSummaryIdx]?.summary : undefined;

  // Slice everything AFTER the prior summary, keep only message-type
  // envelopes. These are the ones with both `.id` (envelope) and
  // `.message` (the AgentMessage body) — both needed below.
  const messageEntriesAfterPriorSummary = entriesSnapshot
    .slice(priorSummaryIdx + 1)
    .filter(
      (e): e is AsyncCompactionSessionEntry & { id: string; message: AgentMessage } =>
        e?.type === "message" &&
        typeof e.id === "string" &&
        e.id.length > 0 &&
        e.message != null,
    );

  // Build the snapshot.messages slot using the unwrapped bodies so the
  // existing split / summarize functions keep working unchanged.
  const snapshot: Snapshot = {
    capturedAt: Date.now(),
    capturedTokens: params.totalTokens,
    messages: messageEntriesAfterPriorSummary.map((e) => e.message),
  };

  log.info(
    `[async-compaction] snapshot: session=${state.sessionKey} totalEntries=${entriesSnapshot.length} sincePriorSummary=${messageEntriesAfterPriorSummary.length} priorSummaryChars=${previousSummary?.length ?? 0} capturedTokens=${snapshot.capturedTokens}`,
  );

  // ── SUMMARIZING ──────────────────────────────────────────────────────
  state.phase = "summarizing";
  state.abortController = new AbortController();
  inFlightSummarizers += 1;
  // Hard deadline: abort a hung summarizer so the phase can't wedge and the
  // inFlight slot can't leak. The finally below always clears it and releases
  // the slot, on EVERY exit path (no-op return, throw, or fall-through).
  const summarizerTimeout = setTimeout(() => {
    log.warn(
      `[async-compaction] summarizer exceeded ${SUMMARIZER_TIMEOUT_MS}ms — aborting: session=${state.sessionKey}`,
    );
    try {
      state.abortController?.abort();
    } catch {
      /* already settled */
    }
  }, SUMMARIZER_TIMEOUT_MS);

  const summarizationStart = Date.now();
  let summaryResult: SummaryResult;
  try {
    // Split the post-prior-summary tail into [summarizable, preserved-verbatim]
    // using the token-budget algorithm from commit 8db7cf6. firstKeptEntryId
    // is the envelope id of the first preserved entry — pi's
    // appendCompaction replaces all entries before it with the new
    // compactionSummary marker.
    const messagesAfterPriorSummary = snapshot.messages;
    let { summarizableMessages, preservedMessages } = params.keepRecentTokensBudget > 0
      ? splitPreservedRecentTurnsByTokens({
          messages: messagesAfterPriorSummary,
          keepRecentTokensBudget: params.keepRecentTokensBudget,
          preservedToolResultMaxTokens: params.preservedToolResultMaxTokens,
        })
      : (() => {
          // Fallback: no budget configured — preserve only the last message
          // (legacy behavior). Logged below so we know if this triggers in prod.
          const last = messagesAfterPriorSummary[messagesAfterPriorSummary.length - 1];
          return {
            summarizableMessages: messagesAfterPriorSummary.slice(0, -1),
            preservedMessages: last ? [last] : [],
          };
        })();

    log.info(
      `[async-compaction] split: session=${state.sessionKey} summarizable=${summarizableMessages.length} preservedVerbatim=${preservedMessages.length} budget=${params.keepRecentTokensBudget}`,
    );

    // THE TAIL ITSELF IS OVER BUDGET. This compaction was triggered because
    // the session is past asyncSoftTriggerPct, yet the split kept every
    // entry since the last summary as "recent". That happens when the newest
    // turns are heavy -- above all a large tool result, which the pairing
    // pass keeps without counting against the budget. Skipping here left the
    // session over threshold on every later turn, triggering and skipping
    // until it overflowed (seen on a GO: 82%, 85%, 91%, all "nothing to
    // summarize"). Retry with the smallest tail possible -- the latest
    // assistant step and its tool results -- and summarize everything before
    // it, including the human message that started the current tool loop
    // (the summary carries it). Anchoring on the user+assistant PAIR would
    // keep that whole loop, which is exactly the case that gets stuck.
    // The splice keeps original entries, so summarizing is the only thing
    // that actually removes an oversized result from the session.
    if (summarizableMessages.length === 0 && params.keepRecentTokensBudget > 0) {
      const minimal = splitPreservedRecentTurnsByTokens({
        messages: messagesAfterPriorSummary,
        keepRecentTokensBudget: 1,
        preservedToolResultMaxTokens: params.preservedToolResultMaxTokens,
        anchorAssistantOnly: true,
      });
      if (minimal.summarizableMessages.length > 0) {
        log.info(
          `[async-compaction] tail over budget — retrying with minimal tail: session=${state.sessionKey} summarizable=${minimal.summarizableMessages.length} preservedVerbatim=${minimal.preservedMessages.length}`,
        );
        summarizableMessages = minimal.summarizableMessages;
        preservedMessages = minimal.preservedMessages;
      }
    }

    if (summarizableMessages.length === 0) {
      // Honest no-op: the tail already fits keepRecentTokensBudget, so there's
      // nothing older to summarize — this reduced NOTHING. If the session is
      // still over threshold here, that's a keepRecentTokensPct signal, not a
      // compaction that silently "succeeded" at shrinking context.
      log.info(
        `[async-compaction] nothing to summarize (reduced 0 tokens): session=${state.sessionKey} sincePriorSummary=${messagesAfterPriorSummary.length} all within keepRecent budget — skipping`,
      );
      state.phase = "idle";
      emitAsyncCompactionEvent(state, "end", { ok: true, reason: "no-op", reducedTokens: 0 });
      return;
    }

    if (preservedMessages.length === 0) {
      throw new Error(
        "split returned no preserved messages — refusing to splice the entire history",
      );
    }

    // Map the first preserved message back to its envelope id by reference
    // equality. The split helper uses `.filter()` on the input array so
    // each output element is the SAME object reference as the original —
    // a Set lookup is therefore reliable. messageEntriesAfterPriorSummary
    // is the parallel array of envelopes; messageEntriesAfterPriorSummary[i]
    // corresponds to messagesAfterPriorSummary[i].
    const preservedSet = new Set<AgentMessage>(preservedMessages);
    let firstPreservedMessageIndex = -1;
    for (let i = 0; i < messagesAfterPriorSummary.length; i += 1) {
      if (preservedSet.has(messagesAfterPriorSummary[i])) {
        firstPreservedMessageIndex = i;
        break;
      }
    }
    if (firstPreservedMessageIndex < 0) {
      throw new Error(
        "could not locate first preserved message in snapshot — reference equality lost",
      );
    }
    const firstKeptEntryId =
      messageEntriesAfterPriorSummary[firstPreservedMessageIndex].id;

    log.info(
      `[async-compaction] summarizing: session=${state.sessionKey} input=${summarizableMessages.length} previousSummaryChars=${previousSummary?.length ?? 0} model=${params.model.id ?? "?"}`,
    );

    const summary = await summarizeInStages({
      messages: summarizableMessages,
      model: params.model,
      apiKey: params.apiKey,
      signal: state.abortController.signal,
      reserveTokens: params.reserveTokens,
      maxChunkTokens: Math.max(1, Math.floor(params.contextWindow * 0.5)),
      contextWindow: params.contextWindow,
      customInstructions: params.customInstructions,
      summarizationInstructions: params.summarizationInstructions,
      previousSummary,
    });

    // Data-loss guard. If summarizeInStages's fallback chain bottomed
    // out with a stub (e.g., summarizer model is unreachable — verified
    // 2026-05-26 when the synthetic gemini-2.5-flash-100k id returned
    // 404 and the final fallback produced an 84-char "[Large message]"
    // marker that then REPLACED 47 messages of real conversation),
    // refuse to splice. The session keeps its full history; next trigger
    // retries the LLM call.
    const MIN_USEFUL_SUMMARY_CHARS = 500;
    if (summary.length < MIN_USEFUL_SUMMARY_CHARS) {
      throw new Error(
        `summarizer produced suspiciously short output (${summary.length} chars < ${MIN_USEFUL_SUMMARY_CHARS}); refusing to splice ` +
          `${summarizableMessages.length} messages into a stub. ` +
          `Check the summarizer model is reachable (agents.defaults.compaction.model).`,
      );
    }

    // PROVISIONAL post-splice estimate from the SNAPSHOT populations. It is
    // OVERWRITTEN at splice time (below) with the true size measured from the
    // live retained entries: the snapshot math ignores turns that accreted
    // during the window, so on its own it writes a fictionally-low totalTokens
    // (which is exactly why the next trigger never re-fired and context stayed
    // high).
    const summarizableTokens = estimateMessagesTokens(summarizableMessages);
    const summaryTokens = Math.ceil(summary.length / 4);
    const tokensAfter = Math.max(
      0,
      Math.floor(snapshot.capturedTokens - summarizableTokens) + summaryTokens,
    );
    summaryResult = {
      summary,
      firstKeptEntryId,
      tokensBefore: snapshot.capturedTokens,
      tokensAfter,
      summarizationMs: Date.now() - summarizationStart,
    };
    log.info(
      `[async-compaction] summary done: session=${state.sessionKey} chars=${summary.length} elapsedMs=${summaryResult.summarizationMs}`,
    );
  } catch (err) {
    handlePipelineFailure({
      state,
      errorMessage: err instanceof Error ? err.message : String(err),
      maxFailedAttempts: cfg?.asyncMaxFailedAttempts ?? 2,
      context: "summarize",
    });
    return;
  } finally {
    clearTimeout(summarizerTimeout);
    inFlightSummarizers -= 1;
  }

  // ── SPLICING ─────────────────────────────────────────────────────────
  state.phase = "splicing";

  // Verify firstKeptEntryId is still in the live session before splicing.
  // Entries are append-only, so this should always succeed unless the
  // session has been completely recreated since snapshot.
  const liveEntriesAtSplice = params.getEntries();
  if (!liveEntriesAtSplice || !Array.isArray(liveEntriesAtSplice)) {
    handlePipelineFailure({
      state,
      errorMessage: `getEntries() returned non-array at splice time (${typeof liveEntriesAtSplice})`,
      maxFailedAttempts: cfg?.asyncMaxFailedAttempts ?? 2,
      context: "splice-getEntries",
    });
    return;
  }
  const liveIds = new Set(
    liveEntriesAtSplice.map((e) => e?.id).filter((id): id is string => typeof id === "string" && id.length > 0),
  );
  if (!liveIds.has(summaryResult.firstKeptEntryId)) {
    handlePipelineFailure({
      state,
      errorMessage: `firstKeptEntryId not in live session at splice time (${summaryResult.firstKeptEntryId})`,
      maxFailedAttempts: cfg?.asyncMaxFailedAttempts ?? 2,
      context: "splice-id-check",
    });
    return;
  }

  // TRUTHFUL post-splice size, measured from the LIVE retained set: the new
  // summary marker PLUS every entry kept from firstKeptEntryId onward (which
  // includes the turns that accreted during the window). Overwrites the
  // snapshot-based provisional above. With an honest totalTokens, the existing
  // per-turn trigger re-fires while the session is still over threshold and
  // folds the accreted turns into the cumulative summary over the next turn or
  // two — lossless, because nothing is dropped until a summary already covers
  // it. (firstKeptEntryId is guaranteed present — checked just above.)
  const firstKeptIndex = liveEntriesAtSplice.findIndex(
    (e) => e?.id === summaryResult.firstKeptEntryId,
  );
  const keptMessages =
    firstKeptIndex >= 0
      ? liveEntriesAtSplice
          .slice(firstKeptIndex)
          .map((e) => e?.message)
          .filter((m): m is AgentMessage => !!m)
      : [];
  const messagesKeptAfterSplice = keptMessages.length;
  summaryResult.tokensAfter =
    Math.ceil(summaryResult.summary.length / 4) +
    estimateMessagesTokens(keptMessages);
  try {
    params.sessionManager.appendCompaction(
      summaryResult.summary,
      summaryResult.firstKeptEntryId,
      summaryResult.tokensBefore,
      {
        asyncTriggered: true,
        summarizationMs: summaryResult.summarizationMs,
        messagesKeptAfterSplice,
      },
      true, // fromHook
    );
    state.phase = "idle";
    state.succeeded += 1;
    // Reset consecutive-failure counter on success. The DISABLED cap
    // (asyncMaxFailedAttempts) is for CONSECUTIVE failures only — one
    // successful splice clears the slate.
    const failedAtSuccessReset = state.failed;
    state.failed = 0;
    state.lastError = undefined;
    log.info(
      `[async-compaction] SPLICED: session=${state.sessionKey} summary=${summaryResult.summary.length}c tokensBefore=${summaryResult.tokensBefore} tokensAfter=${summaryResult.tokensAfter} messagesKeptAfterSplice=${messagesKeptAfterSplice} totalElapsedMs=${Date.now() - state.startedAt} succeeded#${state.succeeded}${failedAtSuccessReset > 0 ? ` consecutiveFailuresResetFrom=${failedAtSuccessReset}` : ""}`,
    );
    // Splice committed — context is now reduced. Tell subscribers (chat
    // dispatcher on the control panel) it's safe to dispatch new turns.
    emitAsyncCompactionEvent(state, "end", {
      ok: true,
      tokensBefore: summaryResult.tokensBefore,
      tokensAfter: summaryResult.tokensAfter,
      summarizationMs: summaryResult.summarizationMs,
      succeeded: state.succeeded,
    });

    // Disk-side cycle reset: bump compactionCount AND clear
    // memoryFlushLastFiredAtTokens=null so the next auto-flush can
    // fire. Pi's appendCompaction doesn't fire the auto_compaction_end
    // event through the path that triggers incrementRunCompactionCount,
    // so without this the disk-side counters never advance for async
    // splices and a stale flush stamp permanently blocks the cycle
    // (verified 2026-05-26 on go-amz19 — stamp=41327 from an older
    // flush kept blocking new auto-flushes even after a clean SPLICED).
    if (params.storePath) {
      try {
        await updateSessionStoreEntry({
          storePath: params.storePath,
          sessionKey: state.sessionKey,
          update: async (entry) => ({
            // Write the reduced post-splice size FIRST (atomically, in the
            // same update) so the instant the gate re-arms below, any
            // reply cycle reads the new low totalTokens instead of the
            // stale pre-splice value. Without this the flush gate runs at
            // the start of the next reply cycle (before the turn refreshes
            // totalTokens), sees the stale high value with the gate freshly
            // open, and spurious-flushes immediately after every compaction.
            totalTokens: summaryResult.tokensAfter,
            totalTokensFresh: true,
            compactionCount: (entry.compactionCount ?? 0) + 1,
            // Reset the memory-flush cycle stamp so the next flush can
            // fire. Mirror of what incrementCompactionCount in
            // session-updates.ts does for the sync safeguard path.
            memoryFlushLastFiredAtTokens: null,
            updatedAt: Date.now(),
          }),
        });
        log.info(
          `[async-compaction] cycle reset: session=${state.sessionKey} compactionCount++ memoryFlushLastFiredAtTokens=null`,
        );
      } catch (resetErr) {
        // Non-fatal. The splice already happened — this is just
        // bookkeeping for the next cycle's gate. Log and continue.
        log.warn(
          `[async-compaction] cycle reset FAILED (compactionCount + flush stamp not updated on disk): session=${state.sessionKey} err=${resetErr instanceof Error ? resetErr.message : String(resetErr)}`,
        );
      }
    } else {
      log.warn(
        `[async-compaction] SPLICED but storePath missing — disk-side compactionCount and flush stamp NOT updated. Caller must pass storePath. session=${state.sessionKey}`,
      );
    }
  } catch (err) {
    handlePipelineFailure({
      state,
      errorMessage: err instanceof Error ? err.message : String(err),
      maxFailedAttempts: cfg?.asyncMaxFailedAttempts ?? 2,
      context: "splice-appendCompaction",
    });
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** Centralized failure handler. Bumps the consecutive-failure counter
 *  and transitions to either FAILED (transient, auto-reset in 30s) or,
 *  past the cap, DISABLED — a SELF-HEALING back-off that re-enables after
 *  DISABLED_COOLDOWN_MS (half-open). Neither state gates agent turns; both
 *  only pace background compaction retries.
 *
 *  Failure count is reset to 0 on any SPLICED success — so the cap
 *  is for consecutive failures, not lifetime. */
function handlePipelineFailure(params: {
  state: AsyncCompactionState;
  errorMessage: string;
  maxFailedAttempts: number;
  context: string; // e.g. "summarize", "splice", "snapshot"
}): void {
  const { state, errorMessage, maxFailedAttempts, context } = params;
  state.failed += 1;
  state.lastError = errorMessage;
  if (state.failed >= maxFailedAttempts) {
    // Back off from RE-TRIGGERING compaction for a cooldown, then re-enable
    // (half-open). This is NOT a turn gate: the agent keeps taking turns
    // throughout (attempt.ts no longer rejects) — async compaction is a
    // background optimization, and the sync safeguard bounds context at the
    // hard limit if a broken summarizer keeps this backed off. Self-healing —
    // the next successful splice resets the counter to 0.
    state.phase = "disabled";
    log.error(
      `[async-compaction] backing off (self-healing): session=${state.sessionKey} consecutiveFailures=${state.failed} cap=${maxFailedAttempts} context=${context} lastError=${errorMessage}. Retrying after ${Math.round(DISABLED_COOLDOWN_MS / 1000)}s; agent turns are unaffected.`,
    );
    // terminal:false — this resumes on its own; the CP dispatcher must not
    // treat it as permanent.
    emitAsyncCompactionEvent(state, "end", {
      ok: false,
      terminal: false,
      reason: context,
      consecutiveFailures: state.failed,
      lastError: errorMessage,
    });
    setTimeout(() => {
      if (state.phase === "disabled") {
        state.phase = "idle";
        log.info(
          `[async-compaction] cooldown elapsed — re-enabling (half-open): session=${state.sessionKey}`,
        );
      }
    }, DISABLED_COOLDOWN_MS);
    return;
  }
  state.phase = "failed";
  log.error(
    `[async-compaction] ${context} FAILED (attempt ${state.failed}/${maxFailedAttempts}): session=${state.sessionKey} err=${errorMessage}`,
  );
  // Failed (transient) — the 30s setTimeout below will reset to idle. From
  // a dispatch perspective the compaction round is over; release the wait
  // so new turns can flow (next trigger fires on the next post-turn check
  // if tokens are still above threshold).
  emitAsyncCompactionEvent(state, "end", {
    ok: false,
    terminal: false,
    reason: context,
    consecutiveFailures: state.failed,
    lastError: errorMessage,
  });
  setTimeout(() => {
    if (state.phase === "failed") state.phase = "idle";
  }, 30_000);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
