import crypto from "node:crypto";
import fs from "node:fs";
import type { AgentMessage } from "@mariozechner/pi-agent-core";
import { resolveBootstrapWarningSignaturesSeen } from "../../agents/bootstrap-budget.js";
import { estimateMessagesTokens } from "../../agents/compaction.js";
import { runWithModelFallback } from "../../agents/model-fallback.js";
import { isCliProvider } from "../../agents/model-selection.js";
import { runEmbeddedPiAgent } from "../../agents/pi-embedded.js";
import { resolveSandboxConfigForAgent, resolveSandboxRuntimeStatus } from "../../agents/sandbox.js";
import {
  deriveContextPromptTokens,
  hasNonzeroUsage,
  normalizeUsage,
  type UsageLike,
} from "../../agents/usage.js";
import type { OpenClawConfig } from "../../config/config.js";
import {
  resolveAgentIdFromSessionKey,
  resolveSessionFilePath,
  resolveSessionFilePathOptions,
  type SessionEntry,
  updateSessionStoreEntry,
} from "../../config/sessions.js";
import { logVerbose } from "../../globals.js";
import { registerAgentRunContext } from "../../infra/agent-events.js";
import type { TemplateContext } from "../templating.js";
import type { VerboseLevel } from "../thinking.js";
import type { GetReplyOptions } from "../types.js";
import {
  buildEmbeddedRunBaseParams,
  buildEmbeddedRunContexts,
  resolveModelFallbackOptions,
} from "./agent-runner-utils.js";
import {
  hasAlreadyFlushedForCurrentCompaction,
  resolveMemoryFlushContextWindowTokens,
  resolveMemoryFlushRelativePathForRun,
  resolveMemoryFlushPromptForRun,
  resolveMemoryFlushSettings,
  shouldRunMemoryConsolidation,
} from "./memory-flush.js";
import { resolveAgentWorkspaceDir } from "../../agents/agent-scope.js";
import type { FollowupRun } from "./queue.js";
import { incrementCompactionCount } from "./session-updates.js";
import { acquireConsolidationLock } from "./memory-consolidation-lock.js";

/** Min interval between two STANDALONE memory consolidations for the same
 *  session. Standalone consolidation is evaluated on every turn (size-
 *  triggered), so without a cooldown a still-over-cap MEMORY.md would fire
 *  a consolidation turn every single turn. 10 minutes gives the rewrite time
 *  to land and the agent time to do real work between attempts. */
export const STANDALONE_CONSOLIDATION_MIN_INTERVAL_MS = 10 * 60 * 1000;

export function estimatePromptTokensForMemoryFlush(prompt?: string): number | undefined {
  const trimmed = prompt?.trim();
  if (!trimmed) {
    return undefined;
  }
  const message: AgentMessage = { role: "user", content: trimmed, timestamp: Date.now() };
  const tokens = estimateMessagesTokens([message]);
  if (!Number.isFinite(tokens) || tokens <= 0) {
    return undefined;
  }
  return Math.ceil(tokens);
}

export function resolveEffectivePromptTokens(
  basePromptTokens?: number,
  lastOutputTokens?: number,
  promptTokenEstimate?: number,
): number {
  const base = Math.max(0, basePromptTokens ?? 0);
  const output = Math.max(0, lastOutputTokens ?? 0);
  const estimate = Math.max(0, promptTokenEstimate ?? 0);
  // Flush gating projects the next input context by adding the previous
  // completion and the current user prompt estimate.
  return base + output + estimate;
}

export type SessionTranscriptUsageSnapshot = {
  promptTokens?: number;
  outputTokens?: number;
};

// Keep a generous near-threshold window so large assistant outputs still trigger
// transcript reads in time to flip memory-flush gating when needed.
const TRANSCRIPT_OUTPUT_READ_BUFFER_TOKENS = 8192;
const TRANSCRIPT_TAIL_CHUNK_BYTES = 64 * 1024;

function parseUsageFromTranscriptLine(line: string): ReturnType<typeof normalizeUsage> | undefined {
  const trimmed = line.trim();
  if (!trimmed) {
    return undefined;
  }
  try {
    const parsed = JSON.parse(trimmed) as {
      message?: { usage?: UsageLike };
      usage?: UsageLike;
    };
    const usageRaw = parsed.message?.usage ?? parsed.usage;
    const usage = normalizeUsage(usageRaw);
    if (usage && hasNonzeroUsage(usage)) {
      return usage;
    }
  } catch {
    // ignore bad lines
  }
  return undefined;
}

function resolveSessionLogPath(
  sessionId?: string,
  sessionEntry?: SessionEntry,
  sessionKey?: string,
  opts?: { storePath?: string },
): string | undefined {
  if (!sessionId) {
    return undefined;
  }

  try {
    const transcriptPath = (
      sessionEntry as (SessionEntry & { transcriptPath?: string }) | undefined
    )?.transcriptPath?.trim();
    const sessionFile = sessionEntry?.sessionFile?.trim() || transcriptPath;
    const agentId = resolveAgentIdFromSessionKey(sessionKey);
    const pathOpts = resolveSessionFilePathOptions({
      agentId,
      storePath: opts?.storePath,
    });
    // Normalize sessionFile through resolveSessionFilePath so relative entries
    // are resolved against the sessions dir/store layout, not process.cwd().
    return resolveSessionFilePath(
      sessionId,
      sessionFile ? { sessionFile } : sessionEntry,
      pathOpts,
    );
  } catch {
    return undefined;
  }
}

function deriveTranscriptUsageSnapshot(
  usage: ReturnType<typeof normalizeUsage> | undefined,
): SessionTranscriptUsageSnapshot | undefined {
  if (!usage) {
    return undefined;
  }
  // Prompt portion of context = max(input, cacheRead) + cacheWrite.
  // max() avoids the Gemini cache oscillation (cacheRead ⊆ input, so a
  // plain input+cacheRead+cacheWrite fired the flush ~6K early on
  // go-amz19) while still counting Anthropic's bootstrap, which lives in
  // cacheWrite/cacheRead disjoint from input (input alone read 6 tokens
  // on go-tes1a). This is the prompt base only — resolveEffectivePrompt
  // Tokens adds output (+ a small next-turn estimate) on top.
  const promptTokens = deriveContextPromptTokens(usage);
  const outputRaw = usage.output;
  const outputTokens =
    typeof outputRaw === "number" && Number.isFinite(outputRaw) && outputRaw > 0
      ? outputRaw
      : undefined;
  if (!(typeof promptTokens === "number") && !(typeof outputTokens === "number")) {
    return undefined;
  }
  return {
    promptTokens,
    outputTokens,
  };
}

type SessionLogSnapshot = {
  byteSize?: number;
  usage?: SessionTranscriptUsageSnapshot;
};

async function readSessionLogSnapshot(params: {
  sessionId?: string;
  sessionEntry?: SessionEntry;
  sessionKey?: string;
  opts?: { storePath?: string };
  includeByteSize: boolean;
  includeUsage: boolean;
}): Promise<SessionLogSnapshot> {
  const logPath = resolveSessionLogPath(
    params.sessionId,
    params.sessionEntry,
    params.sessionKey,
    params.opts,
  );
  if (!logPath) {
    return {};
  }

  const snapshot: SessionLogSnapshot = {};

  if (params.includeByteSize) {
    try {
      const stat = await fs.promises.stat(logPath);
      const size = Math.floor(stat.size);
      snapshot.byteSize = Number.isFinite(size) && size >= 0 ? size : undefined;
    } catch {
      snapshot.byteSize = undefined;
    }
  }

  if (params.includeUsage) {
    try {
      const lastUsage = await readLastNonzeroUsageFromSessionLog(logPath);
      snapshot.usage = deriveTranscriptUsageSnapshot(lastUsage);
    } catch {
      snapshot.usage = undefined;
    }
  }

  return snapshot;
}

async function readLastNonzeroUsageFromSessionLog(logPath: string) {
  const handle = await fs.promises.open(logPath, "r");
  try {
    const stat = await handle.stat();
    let position = stat.size;
    let leadingPartial = "";
    while (position > 0) {
      const chunkSize = Math.min(TRANSCRIPT_TAIL_CHUNK_BYTES, position);
      const start = position - chunkSize;
      const buffer = Buffer.allocUnsafe(chunkSize);
      const { bytesRead } = await handle.read(buffer, 0, chunkSize, start);
      if (bytesRead <= 0) {
        break;
      }
      const chunk = buffer.toString("utf-8", 0, bytesRead);
      const combined = `${chunk}${leadingPartial}`;
      const lines = combined.split(/\n+/);
      leadingPartial = lines.shift() ?? "";
      for (let i = lines.length - 1; i >= 0; i -= 1) {
        const usage = parseUsageFromTranscriptLine(lines[i] ?? "");
        if (usage) {
          return usage;
        }
      }
      position = start;
    }
    return parseUsageFromTranscriptLine(leadingPartial);
  } finally {
    await handle.close();
  }
}

export async function readPromptTokensFromSessionLog(
  sessionId?: string,
  sessionEntry?: SessionEntry,
  sessionKey?: string,
  opts?: { storePath?: string },
): Promise<SessionTranscriptUsageSnapshot | undefined> {
  const snapshot = await readSessionLogSnapshot({
    sessionId,
    sessionEntry,
    sessionKey,
    opts,
    includeByteSize: false,
    includeUsage: true,
  });
  return snapshot.usage;
}

export async function runMemoryFlushIfNeeded(params: {
  cfg: OpenClawConfig;
  followupRun: FollowupRun;
  promptForEstimate?: string;
  sessionCtx: TemplateContext;
  opts?: GetReplyOptions;
  defaultModel: string;
  agentCfgContextTokens?: number;
  resolvedVerboseLevel: VerboseLevel;
  sessionEntry?: SessionEntry;
  sessionStore?: Record<string, SessionEntry>;
  sessionKey?: string;
  storePath?: string;
  isHeartbeat: boolean;
  /** STANDALONE consolidation mode (per-turn, size-triggered). When true this
   *  function ONLY rewrites MEMORY.md when it's over cap — it never runs an
   *  append flush, it BYPASSES the once-per-compaction-cycle guard (that gate
   *  belongs to the compaction-driven flush, not to us), and it gates on the
   *  memoryConsolidationAt cooldown instead. Invoked from
   *  maybeRunStandaloneConsolidation after each turn. */
  standaloneConsolidationOnly?: boolean;
}): Promise<SessionEntry | undefined> {
  // Resolve agentId from sessionKey so resolveMemoryFlushSettings can
  // pick up per-agent compaction prompt overrides. sessionKey format is
  // `agent:<agentId>:<rest>`, parsed by the same helper used elsewhere.
  let memoryFlushAgentId: string | undefined;
  if (params.sessionKey) {
    try {
      const { resolveAgentIdFromSessionKey } = await import(
        "../../routing/session-key.js"
      );
      memoryFlushAgentId = resolveAgentIdFromSessionKey(params.sessionKey) || undefined;
    } catch {
      memoryFlushAgentId = undefined;
    }
  }
  // Thread modelId through so reserveTokensPct (percentage-of-contextWindow
  // reserve) can resolve to a concrete absolute floor for this model.
  const memoryFlushModelId = params.followupRun?.run?.model ?? params.defaultModel;
  const memoryFlushSettings = resolveMemoryFlushSettings(
    params.cfg,
    memoryFlushAgentId,
    memoryFlushModelId,
  );
  if (!memoryFlushSettings) {
    return params.sessionEntry;
  }

  const memoryFlushWritable = (() => {
    if (!params.sessionKey) {
      return true;
    }
    const runtime = resolveSandboxRuntimeStatus({
      cfg: params.cfg,
      sessionKey: params.sessionKey,
    });
    if (!runtime.sandboxed) {
      return true;
    }
    const sandboxCfg = resolveSandboxConfigForAgent(params.cfg, runtime.agentId);
    return sandboxCfg.workspaceAccess === "rw";
  })();

  const isCli = isCliProvider(params.followupRun.run.provider, params.cfg);
  const canAttemptFlush = memoryFlushWritable && !params.isHeartbeat && !isCli;
  let entry =
    params.sessionEntry ??
    (params.sessionKey ? params.sessionStore?.[params.sessionKey] : undefined);

  // Standalone consolidation runs per-turn, so short-circuit cheaply before
  // any of the flush token-machinery below: bail if writes are off and apply
  // the min-interval cooldown so a still-over-cap MEMORY.md doesn't fire a
  // consolidation turn every turn. The size trigger itself is evaluated later
  // via shouldRunMemoryConsolidation (consolidationCheck).
  if (params.standaloneConsolidationOnly) {
    if (!canAttemptFlush) {
      return entry ?? params.sessionEntry;
    }
    const lastConsolidationAt = entry?.memoryConsolidationAt ?? 0;
    if (
      lastConsolidationAt &&
      Date.now() - lastConsolidationAt < STANDALONE_CONSOLIDATION_MIN_INTERVAL_MS
    ) {
      return entry ?? params.sessionEntry;
    }
  }
  const contextWindowTokens = resolveMemoryFlushContextWindowTokens({
    modelId: params.followupRun.run.model ?? params.defaultModel,
    agentCfgContextTokens: params.agentCfgContextTokens,
    cfg: params.cfg,
  });

  const promptTokenEstimate = estimatePromptTokensForMemoryFlush(
    params.promptForEstimate ?? params.followupRun.prompt,
  );
  const persistedPromptTokensRaw = entry?.totalTokens;
  const persistedPromptTokens =
    typeof persistedPromptTokensRaw === "number" &&
    Number.isFinite(persistedPromptTokensRaw) &&
    persistedPromptTokensRaw > 0
      ? persistedPromptTokensRaw
      : undefined;
  const hasFreshPersistedPromptTokens =
    typeof persistedPromptTokens === "number" && entry?.totalTokensFresh === true;

  const flushThreshold =
    contextWindowTokens -
    memoryFlushSettings.reserveTokensFloor -
    memoryFlushSettings.softThresholdTokens;

  // When totals are stale/unknown, derive prompt + last output from transcript so memory
  // flush can still be evaluated against projected next-input size.
  //
  // When totals are fresh, only read the transcript when we're close enough to the
  // threshold that missing the last output tokens could flip the decision.
  const shouldReadTranscriptForOutput =
    canAttemptFlush &&
    entry &&
    hasFreshPersistedPromptTokens &&
    typeof promptTokenEstimate === "number" &&
    Number.isFinite(promptTokenEstimate) &&
    flushThreshold > 0 &&
    (persistedPromptTokens ?? 0) + promptTokenEstimate >=
      flushThreshold - TRANSCRIPT_OUTPUT_READ_BUFFER_TOKENS;

  const shouldReadTranscript = Boolean(
    canAttemptFlush && entry && (!hasFreshPersistedPromptTokens || shouldReadTranscriptForOutput),
  );

  const forceFlushTranscriptBytes = memoryFlushSettings.forceFlushTranscriptBytes;
  const shouldCheckTranscriptSizeForForcedFlush = Boolean(
    canAttemptFlush &&
    entry &&
    Number.isFinite(forceFlushTranscriptBytes) &&
    forceFlushTranscriptBytes > 0,
  );
  // Standalone consolidation only cares about MEMORY.md size, never about the
  // transcript-driven flush thresholds — skip the (per-turn) session-log read.
  const shouldReadSessionLog =
    !params.standaloneConsolidationOnly &&
    (shouldReadTranscript || shouldCheckTranscriptSizeForForcedFlush);
  const sessionLogSnapshot = shouldReadSessionLog
    ? await readSessionLogSnapshot({
        sessionId: params.followupRun.run.sessionId,
        sessionEntry: entry,
        sessionKey: params.sessionKey ?? params.followupRun.run.sessionKey,
        opts: { storePath: params.storePath },
        includeByteSize: shouldCheckTranscriptSizeForForcedFlush,
        includeUsage: shouldReadTranscript,
      })
    : undefined;
  const transcriptByteSize = sessionLogSnapshot?.byteSize;
  const shouldForceFlushByTranscriptSize =
    typeof transcriptByteSize === "number" && transcriptByteSize >= forceFlushTranscriptBytes;

  const transcriptUsageSnapshot = sessionLogSnapshot?.usage;
  const transcriptPromptTokens = transcriptUsageSnapshot?.promptTokens;
  const transcriptOutputTokens = transcriptUsageSnapshot?.outputTokens;
  const hasReliableTranscriptPromptTokens =
    typeof transcriptPromptTokens === "number" &&
    Number.isFinite(transcriptPromptTokens) &&
    transcriptPromptTokens > 0;
  const shouldPersistTranscriptPromptTokens =
    hasReliableTranscriptPromptTokens &&
    (!hasFreshPersistedPromptTokens ||
      (transcriptPromptTokens ?? 0) > (persistedPromptTokens ?? 0));

  if (entry && shouldPersistTranscriptPromptTokens) {
    const nextEntry = {
      ...entry,
      totalTokens: transcriptPromptTokens,
      totalTokensFresh: true,
    };
    entry = nextEntry;
    if (params.sessionKey && params.sessionStore) {
      params.sessionStore[params.sessionKey] = nextEntry;
    }
    if (params.storePath && params.sessionKey) {
      try {
        const updatedEntry = await updateSessionStoreEntry({
          storePath: params.storePath,
          sessionKey: params.sessionKey,
          update: async () => ({ totalTokens: transcriptPromptTokens, totalTokensFresh: true }),
        });
        if (updatedEntry) {
          entry = updatedEntry;
          if (params.sessionStore) {
            params.sessionStore[params.sessionKey] = updatedEntry;
          }
        }
      } catch (err) {
        logVerbose(`failed to persist derived prompt totalTokens: ${String(err)}`);
      }
    }
  }

  const promptTokensSnapshot = Math.max(
    hasFreshPersistedPromptTokens ? (persistedPromptTokens ?? 0) : 0,
    hasReliableTranscriptPromptTokens ? (transcriptPromptTokens ?? 0) : 0,
  );
  const hasFreshPromptTokensSnapshot =
    promptTokensSnapshot > 0 &&
    (hasFreshPersistedPromptTokens || hasReliableTranscriptPromptTokens);

  const projectedTokenCount = hasFreshPromptTokensSnapshot
    ? resolveEffectivePromptTokens(
        promptTokensSnapshot,
        transcriptOutputTokens,
        promptTokenEstimate,
      )
    : undefined;
  const tokenCountForFlush =
    typeof projectedTokenCount === "number" &&
    Number.isFinite(projectedTokenCount) &&
    projectedTokenCount > 0
      ? projectedTokenCount
      : undefined;

  // Diagnostic logging to understand why memory flush may not trigger.
  logVerbose(
    `memoryFlush check: sessionKey=${params.sessionKey} ` +
      `tokenCount=${tokenCountForFlush ?? "undefined"} ` +
      `contextWindow=${contextWindowTokens} threshold=${flushThreshold} ` +
      `isHeartbeat=${params.isHeartbeat} isCli=${isCli} memoryFlushWritable=${memoryFlushWritable} ` +
      `compactionCount=${entry?.compactionCount ?? 0} memoryFlushCompactionCount=${entry?.memoryFlushCompactionCount ?? "undefined"} ` +
      `persistedPromptTokens=${persistedPromptTokens ?? "undefined"} persistedFresh=${entry?.totalTokensFresh === true} ` +
      `promptTokensEst=${promptTokenEstimate ?? "undefined"} transcriptPromptTokens=${transcriptPromptTokens ?? "undefined"} transcriptOutputTokens=${transcriptOutputTokens ?? "undefined"} ` +
      `projectedTokenCount=${projectedTokenCount ?? "undefined"} transcriptBytes=${transcriptByteSize ?? "undefined"} ` +
      `forceFlushTranscriptBytes=${forceFlushTranscriptBytes} forceFlushByTranscriptSize=${shouldForceFlushByTranscriptSize}`,
  );

  // MEMORY.md auto-consolidation check: fires when the workspace's
  // MEMORY.md grows past memoryFileMaxChars. Independent of token-based
  // flush trigger — both can fire on the same turn, but consolidation
  // wins for prompt selection (it has a separate, narrower prompt).
  const sessionAgentIdForWorkspace = params.sessionKey
    ? resolveAgentIdFromSessionKey(params.sessionKey)
    : undefined;
  const workspaceDir = sessionAgentIdForWorkspace
    ? resolveAgentWorkspaceDir(params.cfg, sessionAgentIdForWorkspace)
    : undefined;
  const consolidationCheck = memoryFlushSettings
    ? shouldRunMemoryConsolidation({
        workspaceDir,
        memoryFileMaxChars: memoryFlushSettings.memoryFileMaxChars,
      })
    : { trigger: false, sizeChars: 0 };
  const consolidationEligible =
    consolidationCheck.trigger &&
    memoryFlushSettings != null &&
    memoryFlushWritable &&
    !params.isHeartbeat &&
    !isCli &&
    entry != null &&
    // Standalone consolidation has its own memoryConsolidationAt cooldown
    // (checked above), so it deliberately bypasses the once-per-compaction
    // flush-cycle guard — that gate belongs to the compaction-driven flush.
    (params.standaloneConsolidationOnly || !hasAlreadyFlushedForCurrentCompaction(entry));
  // Only fire consolidation once per compaction cycle (uses the same
  // memoryFlushCompactionCount guard as regular flush; we reuse the
  // existing marker because the goal is the same: don't loop on the
  // same trigger condition within one compaction cycle).
  const useConsolidationPrompts =
    consolidationEligible &&
    Boolean(memoryFlushSettings?.consolidationPrompt?.trim());

  // Standalone mode does ONE thing: rewrite an over-cap MEMORY.md. If this
  // turn isn't a real consolidation (under cap, or no consolidation prompt
  // configured), do nothing — never fall through to an append flush.
  if (params.standaloneConsolidationOnly && !useConsolidationPrompts) {
    return entry ?? params.sessionEntry;
  }

  // 2026-05-27: this runs ONLY from the async compaction job (flush-first,
  // once per compaction cycle), so there's no token threshold, counter
  // gate, or transcript-size force-flush anymore — the single
  // async-compaction trigger owns the timing. We still skip when the
  // workspace isn't writable, on heartbeats, or for CLI providers.
  if (!canAttemptFlush) {
    return entry ?? params.sessionEntry;
  }

  if (useConsolidationPrompts) {
    logVerbose(
      `memoryConsolidation triggered: sessionKey=${params.sessionKey} ` +
        `memoryFileChars=${consolidationCheck.sizeChars} ` +
        `memoryFileMaxChars=${memoryFlushSettings?.memoryFileMaxChars ?? "?"} ` +
        `target=${memoryFlushSettings?.memoryFileTargetChars ?? "?"}`,
    );
  }
  const effectivePrompt =
    useConsolidationPrompts && memoryFlushSettings?.consolidationPrompt
      ? memoryFlushSettings.consolidationPrompt
      : memoryFlushSettings?.prompt;
  const effectiveSystemPrompt =
    useConsolidationPrompts && memoryFlushSettings?.consolidationSystemPrompt
      ? memoryFlushSettings.consolidationSystemPrompt
      : memoryFlushSettings?.systemPrompt;

  logVerbose(
    `memoryFlush triggered: sessionKey=${params.sessionKey} tokenCount=${tokenCountForFlush ?? "undefined"} threshold=${flushThreshold}`,
  );

  let activeSessionEntry = entry ?? params.sessionEntry;
  const activeSessionStore = params.sessionStore;
  let bootstrapPromptWarningSignaturesSeen = resolveBootstrapWarningSignaturesSeen(
    activeSessionEntry?.systemPromptReport ??
      (params.sessionKey ? activeSessionStore?.[params.sessionKey]?.systemPromptReport : undefined),
  );
  const flushRunId = crypto.randomUUID();
  if (params.sessionKey) {
    registerAgentRunContext(flushRunId, {
      sessionKey: params.sessionKey,
      verboseLevel: params.resolvedVerboseLevel,
    });
  }
  let memoryCompactionCompleted = false;
  const memoryFlushNowMs = Date.now();
  const memoryFlushWritePath = resolveMemoryFlushRelativePathForRun({
    cfg: params.cfg,
    nowMs: memoryFlushNowMs,
  });
  const flushSystemPrompt = [
    params.followupRun.run.extraSystemPrompt,
    effectiveSystemPrompt ?? memoryFlushSettings.systemPrompt,
  ]
    .filter(Boolean)
    .join("\n\n");
  // ONE CONSOLIDATION AT A TIME PER WORKSPACE. An agent has one
  // workspace and one MEMORY.md but a SESSION PER THREAD, so N threads
  // drive N independent turn loops over one file. A flush is
  // append-only and interleaves safely; a consolidation REWRITES the
  // whole file, so two at once means the slower one writes back a
  // document built from what it read before the other one's rewrite
  // existed — a silent lost update of everything the other kept.
  //
  // The existing cooldown is stored on the SESSION entry, so it spaces
  // out consolidations within one thread and says nothing about two
  // threads firing together. That was rare until the Goal Orchestrator
  // started holding a thread per live autonomous errand.
  //
  // SKIPPED, NOT QUEUED: see the lock module. A waiter would redo the
  // same lost update one step later; the size trigger is
  // level-triggered, so a still-over-cap file fires again next turn.
  let releaseConsolidationLock: (() => void) | null = null;
  if (useConsolidationPrompts) {
    releaseConsolidationLock = acquireConsolidationLock(workspaceDir);
    if (!releaseConsolidationLock) {
      logVerbose(
        `memoryFlush: consolidation already running for ${workspaceDir} — ` +
          `skipping this turn (sessionKey=${params.sessionKey})`,
      );
      return entry ?? params.sessionEntry;
    }
  }

  // ACQUIRED HERE AND NOWHERE EARLIER. The first draft took the lock
  // next to the decision that needs it, which is further up — and the
  // `!canAttemptFlush` guard between the two returns without releasing,
  // stranding the workspace for the life of the process. Nothing
  // between this line and the `try` can return or throw, so the
  // `finally` below is reached on every path that gets past here.
  try {
    await runWithModelFallback({
      ...resolveModelFallbackOptions(params.followupRun.run),
      runId: flushRunId,
      run: async (provider, model, runOptions) => {
        const { authProfile, embeddedContext, senderContext } = buildEmbeddedRunContexts({
          run: params.followupRun.run,
          sessionCtx: params.sessionCtx,
          hasRepliedRef: params.opts?.hasRepliedRef,
          provider,
        });
        const runBaseParams = buildEmbeddedRunBaseParams({
          run: params.followupRun.run,
          provider,
          model,
          runId: flushRunId,
          authProfile,
          allowTransientCooldownProbe: runOptions?.allowTransientCooldownProbe,
        });
        const result = await runEmbeddedPiAgent({
          ...embeddedContext,
          ...senderContext,
          ...runBaseParams,
          // Distinguish flush (append-only writes) from consolidation
          // (intentional full rewrite of MEMORY.md to shrink under cap).
          // Both share dispatch, only the trigger value + prompts differ;
          // pi-tools.ts gates the append-only wrap on "memory" only.
          trigger: useConsolidationPrompts ? "memory_consolidation" : "memory",
          // Path is meaningful only for flush; consolidation writes the
          // full new MEMORY.md content (replace), so leave undefined.
          memoryFlushWritePath: useConsolidationPrompts ? undefined : memoryFlushWritePath,
          prompt: resolveMemoryFlushPromptForRun({
            prompt: effectivePrompt ?? memoryFlushSettings.prompt,
            cfg: params.cfg,
            nowMs: memoryFlushNowMs,
          }),
          extraSystemPrompt: flushSystemPrompt,
          bootstrapPromptWarningSignaturesSeen,
          bootstrapPromptWarningSignature:
            bootstrapPromptWarningSignaturesSeen[bootstrapPromptWarningSignaturesSeen.length - 1],
          onAgentEvent: (evt) => {
            if (evt.stream === "compaction") {
              const phase = typeof evt.data.phase === "string" ? evt.data.phase : "";
              if (phase === "end") {
                memoryCompactionCompleted = true;
              }
            }
          },
        });
        bootstrapPromptWarningSignaturesSeen = resolveBootstrapWarningSignaturesSeen(
          result.meta?.systemPromptReport,
        );
        return result;
      },
    });
    let memoryFlushCompactionCount =
      activeSessionEntry?.compactionCount ??
      (params.sessionKey ? activeSessionStore?.[params.sessionKey]?.compactionCount : 0) ??
      0;
    if (memoryCompactionCompleted && !params.standaloneConsolidationOnly) {
      const nextCount = await incrementCompactionCount({
        sessionEntry: activeSessionEntry,
        sessionStore: activeSessionStore,
        sessionKey: params.sessionKey,
        storePath: params.storePath,
      });
      if (typeof nextCount === "number") {
        memoryFlushCompactionCount = nextCount;
      }
    }
    if (params.storePath && params.sessionKey) {
      try {
        // Counter-based gate (2026-05-26): increment memoryFlushCount on
        // every successful flush. shouldRunMemoryFlush blocks the next
        // flush while memoryFlushCount > compactionCount, so each cycle
        // fires at most one flush. The next compaction ticks
        // compactionCount, re-arming the gate.
        const updatedEntry = await updateSessionStoreEntry({
          storePath: params.storePath,
          sessionKey: params.sessionKey,
          // Standalone consolidation stamps only memoryConsolidationAt (its
          // own cooldown clock); it must NOT touch memoryFlushCount/
          // memoryFlushCompactionCount, which gate the compaction-driven flush.
          update: async (entry) =>
            params.standaloneConsolidationOnly
              ? { memoryConsolidationAt: Date.now() }
              : {
                  memoryFlushAt: Date.now(),
                  memoryFlushCompactionCount,
                  memoryFlushCount: (entry.memoryFlushCount ?? 0) + 1,
                },
        });
        if (updatedEntry) {
          activeSessionEntry = updatedEntry;
        }
      } catch (err) {
        logVerbose(`failed to persist memory flush metadata: ${String(err)}`);
      }
    }
  } catch (err) {
    logVerbose(`memory flush run failed: ${String(err)}`);
  } finally {
    // ALWAYS, and from a finally rather than the happy path: a stranded
    // holder disables consolidation for that agent until the gateway
    // restarts, and the symptom — a MEMORY.md that grows for ever and
    // never consolidates — looks nothing like a lock bug.
    releaseConsolidationLock?.();
  }

  return activeSessionEntry;
}
